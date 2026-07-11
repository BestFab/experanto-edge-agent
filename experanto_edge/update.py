"""OTA updates — two scopes, both server-initiated:

  * update_agent  — the Experanto Edge program itself (signed release + rollback)
  * update_system — the Raspberry Pi OS / packages (apt)

Trust model
-----------
The agent runs unprivileged and sandboxed (systemd ProtectSystem=strict): it can
neither write its own venv under /opt nor run apt. So the split is:

  agent (this module, unprivileged)
    download manifest + artifact from `update_base_url`
    verify sha256 + Ed25519 signature against `update_public_key`   <-- trust gate
    stage the verified artifact under the state dir
    launch the root helper DETACHED (sudo -n ota_helper ...) and ack immediately

  ota-helper.sh (root, via a one-line NOPASSWD sudoers rule)
    install the new release into releases/{version}/venv
    probe it (`experanto-edge --selfcheck`)  <-- pre-swap health-check
    atomically repoint  {app_dir}/current -> releases/{version}  and restart
    post-restart: verify the new agent is healthy (fresh health marker),
                  otherwise roll the symlink back and restart the old release

Only a release whose signature verifies here ever reaches the privileged helper.
Verification lives in the agent (robust Python); the helper trusts the staged,
already-verified artifact (a compromised agent is already game over).
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import subprocess
import time
from typing import Optional, Tuple

log = logging.getLogger("experanto-edge.update")

_CHUNK = 64 * 1024
_DOWNLOAD_TIMEOUT = 120


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable, no privilege / no network side effects here)
# ---------------------------------------------------------------------------

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_signature(public_key_b64: str, message: bytes, signature_b64: str) -> bool:
    """True iff `signature_b64` is a valid Ed25519 signature of `message`.

    public_key_b64: base64 of the raw 32-byte Ed25519 public key.
    Any error (bad key, bad signature, missing lib) -> False (fail closed).
    """
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except Exception as e:  # pragma: no cover - dependency missing
        log.error("cryptography non disponibile: %s", e)
        return False
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        pub.verify(base64.b64decode(signature_b64), message)
        return True
    except InvalidSignature:
        return False
    except Exception as e:
        log.error("verifica firma fallita: %s", e)
        return False


def verify_manifest(manifest: dict, version: str, artifact_path: str,
                    public_key_b64: str) -> Tuple[bool, str]:
    """Check that `artifact_path` matches a manifest signed for exactly `version`.

    manifest = {"version", "sha256", "signature"}; the signature covers the ASCII
    bytes "{version}:{sha256}" so it binds the requested version to the content
    (blocks tampering and version downgrade/swap).
    """
    if not public_key_b64:
        return False, "update_public_key non configurata"
    m_version = str(manifest.get("version", ""))
    m_sha = str(manifest.get("sha256", "")).lower()
    m_sig = manifest.get("signature", "")
    if m_version != str(version):
        return False, f"versione manifest {m_version} != richiesta {version}"
    if not m_sha or not m_sig:
        return False, "manifest incompleto (sha256/signature)"
    actual = _sha256_file(artifact_path).lower()
    if actual != m_sha:
        return False, f"sha256 mismatch (atteso {m_sha[:12]}.., ottenuto {actual[:12]}..)"
    message = f"{m_version}:{m_sha}".encode("ascii")
    if not verify_signature(public_key_b64, message, m_sig):
        return False, "firma Ed25519 non valida"
    return True, "ok"


# ---------------------------------------------------------------------------
# Network (thin wrappers over requests; mocked in tests)
# ---------------------------------------------------------------------------

def _get_json(url: str) -> dict:
    import requests
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json()


def _download(url: str, dest: str) -> None:
    import requests
    with requests.get(url, timeout=_DOWNLOAD_TIMEOUT, stream=True) as r:
        r.raise_for_status()
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(_CHUNK):
                if chunk:
                    f.write(chunk)
        os.replace(tmp, dest)


# ---------------------------------------------------------------------------
# Health marker (written by the running agent; read by the helper post-restart)
# ---------------------------------------------------------------------------

def write_health_marker(cfg) -> None:
    """Best-effort: record that this agent process is alive and cycling."""
    path = getattr(cfg, "health_path", "")
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("%d %s\n" % (int(time.time()), _agent_version()))
    except Exception as e:
        log.debug("health marker non scritto: %s", e)


def _agent_version() -> str:
    from . import __version__
    return __version__


# ---------------------------------------------------------------------------
# OTA entry points (called by the command dispatcher)
# ---------------------------------------------------------------------------

def update_agent(cfg, version: Optional[str]) -> Tuple[bool, str]:
    """Download + verify the signed release `version`, then hand the privileged
    install/swap/restart to the root helper (detached). Returns as soon as the
    helper is launched, so the ack goes out before the service is restarted.
    """
    if not version:
        return False, "versione non specificata"
    if version == _agent_version():
        return True, f"gia' alla versione {version}"
    base = (getattr(cfg, "update_base_url", "") or "").rstrip("/")
    if not base:
        return False, "update_base_url non configurata"

    state_dir = os.path.dirname(getattr(cfg, "buffer_path", "") or
                                "/var/lib/experanto-edge/buffer.db") or "/var/lib/experanto-edge"
    artifact = os.path.join(state_dir, f"experanto-edge-{version}.tar.gz")

    try:
        manifest = _get_json(f"{base}/experanto-edge-{version}.json")
        _download(f"{base}/experanto-edge-{version}.tar.gz", artifact)
    except Exception as e:
        _safe_unlink(artifact)
        return False, f"download fallito: {e}"

    ok, why = verify_manifest(manifest, version, artifact, getattr(cfg, "update_public_key", ""))
    if not ok:
        _safe_unlink(artifact)
        return False, f"release rifiutata: {why}"

    ok, why = _launch_helper(cfg, "agent", version, artifact)
    if not ok:
        _safe_unlink(artifact)
        return False, why
    return True, f"update verificato, installazione avviata per {version}"


def update_system(cfg, mode: str = "security") -> Tuple[bool, str]:
    """Apply OS updates via the root helper. mode: 'security' (default) | 'full'."""
    if mode not in ("security", "full"):
        return False, f"mode non valido: {mode}"
    ok, why = _launch_helper(cfg, "system", mode)
    if not ok:
        return False, why
    return True, f"aggiornamento sistema ({mode}) avviato"


def reboot(cfg) -> Tuple[bool, str]:
    """Reboot the host via the root helper."""
    ok, why = _launch_helper(cfg, "reboot")
    if not ok:
        return False, why
    return True, "reboot avviato"


def _launch_helper(cfg, *args: str) -> Tuple[bool, str]:
    """Launch the root OTA helper detached (survives the agent restart)."""
    helper = getattr(cfg, "ota_helper", "") or ""
    if not helper or not os.path.exists(helper):
        return False, f"ota_helper non trovato: {helper or '(non configurato)'}"
    cmd = ["sudo", "-n", helper, *args]
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,      # detach: agent restart won't kill it
            close_fds=True,
        )
    except Exception as e:
        return False, f"avvio helper fallito: {e}"
    return True, "helper avviato"


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Version reporting
# ---------------------------------------------------------------------------

def os_version() -> str:
    """Best-effort OS description for the status payload."""
    try:
        out = subprocess.run(
            ["lsb_release", "-ds"], capture_output=True, text=True, timeout=5
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return "unknown"
