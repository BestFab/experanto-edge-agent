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
from typing import Any, Optional, Tuple

log = logging.getLogger("experanto-edge.update")

_CHUNK = 64 * 1024
_DOWNLOAD_TIMEOUT = 120
# How long to watch the detached OTA helper before assuming it's underway. A real
# install runs far longer; an immediate `sudo -n` denial exits well within this.
_HELPER_LAUNCH_WINDOW_S = 4
# Broker channel (root path-unit): how long to wait for helper.response before
# concluding the broker isn't installed. The path unit fires on inotify, so a
# working broker answers in well under a second.
_BROKER_WAIT_S = 8.0
_BROKER_POLL_S = 0.2


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

    # Stage nella helper dir: il broker accetta solo artifact confinati li'.
    artifact = os.path.join(_helper_dir(cfg), f"experanto-edge-{version}.tar.gz")

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


def _helper_dir(cfg) -> str:
    """Directory dei file del canale privilegiato (request/response) e dello
    staging dell'artifact OTA.

    DEVE combaciare con `PathExists=` di experanto-edge-helper.path (hardcoded
    /var/lib/experanto-edge) e con lo STATE_DIR del broker. NON derivata da
    `buffer_path`: un `buffer_path` spostato non deve orfanare il canale
    (l'agente scriverebbe dove nessuna path-unit guarda). Override solo via
    `cfg.helper_dir`, che i test usano; in produzione resta il default.
    """
    return getattr(cfg, "helper_dir", "") or "/var/lib/experanto-edge"


def _launch_helper(cfg, *args: str) -> Tuple[bool, str]:
    """Run a privileged helper action, trying the two channels in order:

    1. `sudo -n ota-helper.sh …` — instantaneous where it works (agent run from a
       shell, dev/test Pi). On the fleet the systemd sandbox (NoNewPrivileges)
       denies it in well under a second.
    2. broker — write {state}/helper.request; the root path-unit
       (experanto-edge-helper.path) validates it and runs ota-helper.sh with the
       sandbox intact. This is the fleet's normal path since 0.4.1.

    Both failures are surfaced together so a misprovisioned Pi is diagnosable
    from the ack alone.
    """
    ok, why = _launch_via_sudo(cfg, *args)
    if ok:
        return ok, why
    b_ok, b_why = _launch_via_broker(cfg, *args)
    if b_ok:
        return b_ok, b_why
    return False, f"{why}; {b_why}"


def _helper_resp_dir(cfg) -> str:
    """Dir della response del broker: ROOT-OWNED (default /run/experanto-edge,
    RuntimeDirectory della helper unit). L'agente ci LEGGE soltanto; non deve
    scriverci (e non potrebbe): e' proprio questo a togliere all'attaccante il
    symlink-swap sui file di root. Distinta dalla helper dir (request), che
    l'agente deve poter scrivere."""
    return getattr(cfg, "helper_resp_dir", "") or "/run/experanto-edge"


def _launch_via_broker(cfg, *args: str) -> Tuple[bool, str]:
    """Privileged channel WITHOUT sudo: request/response files brokered by the
    root path-unit. The broker answers `accepted` before long actions (same
    detached contract as the sudo path) and `done` for instant ones (ping).

    La request si scrive nella helper dir (agent-writable); la response si legge
    dalla resp dir root-owned. Correlazione per nonce: una response di un giro
    precedente (nonce diverso) non combacia mai.
    """
    req = os.path.join(_helper_dir(cfg), "helper.request")
    resp = os.path.join(_helper_resp_dir(cfg), "helper.response")
    nonce = "%x-%x" % (int(time.time() * 1000), os.getpid())
    line = " ".join([str(int(time.time())), nonce, *args])
    try:
        tmp = req + ".tmp"
        with open(tmp, "w") as f:
            f.write(line + "\n")
        os.replace(tmp, req)
    except OSError as e:
        return False, f"broker: request non scrivibile: {e}"
    deadline = time.monotonic() + _BROKER_WAIT_S
    while time.monotonic() < deadline:
        try:
            with open(resp) as f:
                parts = f.readline().split(None, 4)
        except OSError:
            time.sleep(_BROKER_POLL_S)
            continue
        if len(parts) >= 4 and parts[1] == nonce:
            _safe_unlink(resp)
            status, rc = parts[2], parts[3]
            detail = parts[4].strip() if len(parts) > 4 else ""
            if status in ("accepted", "done") and rc == "rc=0":
                return True, detail or f"{args[0]} avviato (broker)"
            return False, f"broker: {status} {rc} {detail}".strip()
        time.sleep(_BROKER_POLL_S)
    # Nessuna response col nostro nonce entro la finestra. Due cause possibili:
    # la path-unit non e' installata/attiva, OPPURE il broker e' occupato da
    # un'azione precedente (con systemd-run il broker si libera in <1s, quindi
    # e' raro). Rimuoviamo la request SOLO se e' ancora la nostra: se il broker
    # l'ha gia' consumata (mv), non deve essere ricreata ne' l'esito confuso.
    _safe_unlink(req)
    return False, "broker senza risposta (path-unit assente o occupata)"


def _launch_via_sudo(cfg, *args: str) -> Tuple[bool, str]:
    """Launch the root OTA helper detached (must survive the agent's own restart).

    The helper runs LONG (venv install + selfcheck + swap + service restart), so we
    detach it. But we do NOT blindly assume success: `sudo -n` can be denied *after*
    the spawn (e.g. the systemd unit sets `NoNewPrivileges=yes`, or the scoped sudoers
    rule is missing) — which previously looked like a successful OTA while nothing ran.
    We give it a short window: if it dies fast with a non-zero code, surface the error;
    if it's still running when the window elapses, the install is underway and we detach.
    stderr goes to a file (not a pipe) so the long-running helper can't deadlock on a
    full pipe buffer.
    """
    helper = getattr(cfg, "ota_helper", "") or ""
    if not helper or not os.path.exists(helper):
        return False, f"ota_helper non trovato: {helper or '(non configurato)'}"
    err_path = os.path.join(_helper_dir(cfg), ".ota-helper.err")
    cmd = ["sudo", "-n", helper, *args]
    errf: Any = subprocess.DEVNULL
    try:
        errf = open(err_path, "wb")
    except OSError:
        pass  # non-fatal: we just lose the early error text
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=errf, start_new_session=True, close_fds=True,
        )
    except Exception as e:
        return False, f"avvio helper fallito: {e}"
    finally:
        if errf is not subprocess.DEVNULL:
            errf.close()
    try:
        rc = proc.wait(timeout=_HELPER_LAUNCH_WINDOW_S)
    except subprocess.TimeoutExpired:
        return True, "helper avviato"          # still running = install underway -> detach
    # Exited within the window -> a fast failure (a real install runs far longer).
    detail = ""
    try:
        with open(err_path, "r", errors="replace") as f:
            detail = f.read().strip()[:200]
    except OSError:
        pass
    if rc != 0:
        return False, (f"helper fallito all'avvio (rc={rc}): "
                       f"{detail or 'sudo -n negato? (NoNewPrivileges o regola sudoers mancante)'}")
    return True, "helper eseguito"


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
