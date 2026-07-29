"""E5 — OTA: signature/manifest verification + update_agent orchestration.

Pure/mocked: no privilege, no real network, no systemd. The privileged helper
(ota-helper.sh) and install.sh are shell and only run on a real Pi (verified E2E
in E7); here we prove the agent-side trust gate and the sign<->verify contract.
"""
import base64
import hashlib
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

from experanto_edge import update  # noqa: E402


# --- crypto helpers (server side, mirrors tools/sign_release.py) ---

def _keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(
        priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode()
    return priv, pub_b64


def _sign(priv, message: bytes) -> str:
    return base64.b64encode(priv.sign(message)).decode()


def _artifact(tmp_path, version="0.2.0", body=b"fake-release-tarball-bytes"):
    art = tmp_path / f"experanto-edge-{version}.tar.gz"
    art.write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()
    return str(art), sha


def _manifest(priv, version, sha):
    return {"version": version, "sha256": sha,
            "signature": _sign(priv, f"{version}:{sha}".encode("ascii"))}


# --- verify_signature / verify_manifest ---

def test_verify_signature_roundtrip():
    priv, pub = _keypair()
    msg = b"0.2.0:abc123"
    assert update.verify_signature(pub, msg, _sign(priv, msg)) is True
    assert update.verify_signature(pub, b"tampered", _sign(priv, msg)) is False


def test_verify_manifest_ok(tmp_path):
    priv, pub = _keypair()
    art, sha = _artifact(tmp_path)
    ok, why = update.verify_manifest(_manifest(priv, "0.2.0", sha), "0.2.0", art, pub)
    assert ok, why


def test_verify_manifest_rejects_tampered_artifact(tmp_path):
    priv, pub = _keypair()
    art, sha = _artifact(tmp_path)
    man = _manifest(priv, "0.2.0", sha)
    Path(art).write_bytes(b"different-bytes")  # content changed after signing
    ok, why = update.verify_manifest(man, "0.2.0", art, pub)
    assert not ok and "sha256" in why


def test_verify_manifest_rejects_version_mismatch(tmp_path):
    priv, pub = _keypair()
    art, sha = _artifact(tmp_path, version="0.2.0")
    ok, why = update.verify_manifest(_manifest(priv, "0.2.0", sha), "9.9.9", art, pub)
    assert not ok and "versione" in why


def test_verify_manifest_rejects_wrong_key(tmp_path):
    priv, _ = _keypair()
    _, other_pub = _keypair()
    art, sha = _artifact(tmp_path)
    ok, why = update.verify_manifest(_manifest(priv, "0.2.0", sha), "0.2.0", art, other_pub)
    assert not ok and "firma" in why


def test_verify_manifest_requires_pubkey(tmp_path):
    priv, _ = _keypair()
    art, sha = _artifact(tmp_path)
    ok, why = update.verify_manifest(_manifest(priv, "0.2.0", sha), "0.2.0", art, "")
    assert not ok


# --- update_agent orchestration (mock network + helper) ---

def _cfg(tmp_path, pub, base="https://x/rel"):
    return types.SimpleNamespace(
        update_base_url=base,
        update_public_key=pub,
        buffer_path=str(tmp_path / "buffer.db"),
        helper_dir=str(tmp_path),          # request + artifact staging
        helper_resp_dir=str(tmp_path),     # response (root-owned in prod)
        ota_helper=str(tmp_path / "ota-helper.sh"),
        app_dir=str(tmp_path / "app"),
        health_path=str(tmp_path / "health"),
    )


@pytest.fixture
def signed(monkeypatch, tmp_path):
    """Wire _get_json/_download to serve a correctly signed 0.2.0 release."""
    priv, pub = _keypair()
    version, body = "0.2.0", b"good-release"
    sha = hashlib.sha256(body).hexdigest()
    man = _manifest(priv, version, sha)

    monkeypatch.setattr(update, "_get_json", lambda url: man)
    monkeypatch.setattr(update, "_download", lambda url, dest: Path(dest).write_bytes(body))
    calls = []
    monkeypatch.setattr(update, "_launch_helper",
                        lambda cfg, *a: (calls.append(a) or (True, "helper avviato")))
    monkeypatch.setattr(update, "_agent_version", lambda: "0.1.0")
    return types.SimpleNamespace(pub=pub, version=version, calls=calls)


def test_update_agent_verified_launches_helper(signed, tmp_path):
    cfg = _cfg(tmp_path, signed.pub)
    ok, detail = update.update_agent(cfg, signed.version)
    assert ok, detail
    assert len(signed.calls) == 1
    kind, ver, art = signed.calls[0]
    assert kind == "agent" and ver == "0.2.0"
    assert Path(art).exists() and art.endswith("experanto-edge-0.2.0.tar.gz")


def test_update_agent_bad_signature_never_launches(monkeypatch, tmp_path):
    priv, pub = _keypair()
    _, other_pub = _keypair()          # cfg trusts a DIFFERENT key
    body = b"good-release"
    sha = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(update, "_get_json", lambda url: _manifest(priv, "0.2.0", sha))
    monkeypatch.setattr(update, "_download", lambda url, dest: Path(dest).write_bytes(body))
    calls = []
    monkeypatch.setattr(update, "_launch_helper", lambda cfg, *a: (calls.append(a) or (True, "")))
    monkeypatch.setattr(update, "_agent_version", lambda: "0.1.0")

    cfg = _cfg(tmp_path, other_pub)
    ok, why = update.update_agent(cfg, "0.2.0")
    assert not ok and "rifiutata" in why
    assert calls == []                                   # privileged path NOT reached
    assert not (tmp_path / "experanto-edge-0.2.0.tar.gz").exists()  # artifact cleaned up


def test_update_agent_same_version_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "_agent_version", lambda: "0.2.0")
    ok, why = update.update_agent(_cfg(tmp_path, "x"), "0.2.0")
    assert ok and "gia'" in why


def test_update_agent_missing_base_url(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "_agent_version", lambda: "0.1.0")
    ok, why = update.update_agent(_cfg(tmp_path, "x", base=""), "0.2.0")
    assert not ok and "update_base_url" in why


def test_update_agent_download_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "_agent_version", lambda: "0.1.0")
    def boom(url): raise RuntimeError("404")
    monkeypatch.setattr(update, "_get_json", boom)
    ok, why = update.update_agent(_cfg(tmp_path, "x"), "0.2.0")
    assert not ok and "download" in why


def test_update_agent_no_version():
    ok, why = update.update_agent(types.SimpleNamespace(), None)
    assert not ok


# --- update_system / reboot ---

def test_update_system_launches_helper(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(update, "_launch_helper", lambda cfg, *a: (calls.append(a) or (True, "")))
    ok, _ = update.update_system(_cfg(tmp_path, "x"), "full")
    assert ok and calls == [("system", "full")]


def test_update_system_bad_mode():
    ok, why = update.update_system(types.SimpleNamespace(), "nonsense")
    assert not ok


def test_reboot_launches_helper(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(update, "_launch_helper", lambda cfg, *a: (calls.append(a) or (True, "")))
    ok, _ = update.reboot(_cfg(tmp_path, "x"))
    assert ok and calls == [("reboot",)]


def test_launch_helper_missing_script_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "_BROKER_WAIT_S", 0.2)
    cfg = types.SimpleNamespace(ota_helper=str(tmp_path / "nope.sh"),
                                helper_dir=str(tmp_path), helper_resp_dir=str(tmp_path))
    ok, why = update._launch_helper(cfg, "reboot")
    assert not ok and "ota_helper" in why


def test_launch_helper_surfaces_immediate_denial(tmp_path, monkeypatch):
    # Regression: `sudo -n` can be denied AFTER the spawn (NoNewPrivileges / missing
    # sudoers) — the helper exits fast non-zero. That must be reported, NOT swallowed
    # as a successful OTA (the old bug: any Popen that didn't throw => "success").
    # Senza broker installato il fallback scade e l'errore sudo resta visibile.
    monkeypatch.setattr(update, "_BROKER_WAIT_S", 0.2)
    helper = tmp_path / "ota-helper.sh"; helper.write_text("#!/bin/sh\nexit 13\n")
    cfg = types.SimpleNamespace(ota_helper=str(helper), helper_dir=str(tmp_path),
                                helper_resp_dir=str(tmp_path))

    class FastFail:
        def wait(self, timeout=None):
            return 13                    # exits within the window with an error
    monkeypatch.setattr(update.subprocess, "Popen", lambda *a, **k: FastFail())
    ok, why = update._launch_helper(cfg, "agent", "0.3.0", "art")
    assert not ok and "rc=13" in why and "broker" in why


def test_launch_helper_detaches_when_still_running(tmp_path, monkeypatch):
    # The happy path: a real install runs long -> wait() times out -> we detach and
    # report launched (the helper will restart the agent when it's done).
    helper = tmp_path / "ota-helper.sh"; helper.write_text("#!/bin/sh\nsleep 60\n")
    cfg = types.SimpleNamespace(ota_helper=str(helper), buffer_path=str(tmp_path / "buf.db"))

    class StillRunning:
        def wait(self, timeout=None):
            raise update.subprocess.TimeoutExpired("ota-helper", timeout)
    monkeypatch.setattr(update.subprocess, "Popen", lambda *a, **k: StillRunning())
    ok, why = update._launch_helper(cfg, "agent", "0.3.0", "art")
    assert ok and "avviato" in why


# --- helper broker: canale privilegiato senza sudo (path-unit root) --------


def _fake_broker(state_dir, status="accepted", rc="rc=0", detail="ok"):
    """Thread che emula experanto-edge-helper: consuma helper.request e risponde
    col NONCE della request (il contratto di correlazione del broker vero)."""
    import os
    import threading
    import time as _time

    def run():
        req = os.path.join(str(state_dir), "helper.request")
        resp = os.path.join(str(state_dir), "helper.response")
        for _ in range(300):
            if os.path.exists(req):
                with open(req) as f:
                    ts, nonce = f.readline().split()[:2]
                os.unlink(req)
                with open(resp + ".tmp", "w") as f:
                    f.write(f"{ts} {nonce} {status} {rc} {detail}\n")
                os.replace(resp + ".tmp", resp)
                return
            _time.sleep(0.005)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _no_sudo(monkeypatch, tmp_path):
    """sudo negato istantaneamente (il caso NoNewPrivileges della flotta)."""
    helper = tmp_path / "ota-helper.sh"; helper.write_text("#!/bin/sh\nexit 1\n")

    class Denied:
        def wait(self, timeout=None):
            return 1
    monkeypatch.setattr(update.subprocess, "Popen", lambda *a, **k: Denied())
    return types.SimpleNamespace(ota_helper=str(helper), helper_dir=str(tmp_path),
                                 helper_resp_dir=str(tmp_path))


def test_launch_helper_falls_back_to_broker(tmp_path, monkeypatch):
    # sudo negato (NNP) ma broker attivo -> il comando riesce via request/response.
    cfg = _no_sudo(monkeypatch, tmp_path)
    _fake_broker(tmp_path, status="accepted", detail="reboot avviato")
    ok, why = update._launch_helper(cfg, "reboot")
    assert ok and "reboot avviato" in why


def test_broker_rejected_is_surfaced(tmp_path, monkeypatch):
    cfg = _no_sudo(monkeypatch, tmp_path)
    _fake_broker(tmp_path, status="rejected", rc="rc=64", detail="azione non ammessa")
    ok, why = update._launch_helper(cfg, "reboot")
    assert not ok and "rejected" in why and "azione non ammessa" in why


def test_broker_absent_times_out_and_cleans_request(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(update, "_BROKER_WAIT_S", 0.3)
    cfg = _no_sudo(monkeypatch, tmp_path)
    ok, why = update._launch_helper(cfg, "reboot")
    assert not ok and "broker senza risposta" in why
    # la request orfana NON deve restare sul disco (sarebbe un replay al prossimo boot)
    assert not os.path.exists(tmp_path / "helper.request")


def test_broker_ignores_stale_response_with_other_nonce(tmp_path, monkeypatch):
    # Una response di un giro precedente (nonce diverso) non deve mai combaciare.
    import os
    monkeypatch.setattr(update, "_BROKER_WAIT_S", 0.3)
    cfg = _no_sudo(monkeypatch, tmp_path)
    ok, why = update._launch_via_broker(cfg, "ping")
    assert not ok  # nessun broker: timeout
    (tmp_path / "helper.response").write_text("0 nonce-vecchio done rc=0 pong\n")
    ok, why = update._launch_via_broker(cfg, "ping")
    assert not ok and "broker senza risposta" in why
    assert not os.path.exists(tmp_path / "helper.request")


def test_helper_dir_is_fixed_not_derived_from_buffer_path():
    # La helper dir DEVE combaciare con la path-unit (hardcoded /var/lib/experanto-edge),
    # indipendentemente da un buffer_path spostato — altrimenti il canale si orfana.
    cfg = types.SimpleNamespace(buffer_path="/data/altrove/buffer.db")
    assert update._helper_dir(cfg) == "/var/lib/experanto-edge"
    cfg2 = types.SimpleNamespace(helper_dir="/custom/dir", buffer_path="/x/y.db")
    assert update._helper_dir(cfg2) == "/custom/dir"


# --- sign_release.py tool <-> agent verify roundtrip ---

def test_sign_release_tool_roundtrip(tmp_path, capsys):
    import sign_release
    priv_file = tmp_path / "signing.key"
    assert sign_release.main(["keygen", "--out-priv", str(priv_file)]) == 0
    pub_b64 = capsys.readouterr().out.strip().splitlines()[-1]

    art = tmp_path / "experanto-edge-0.3.0.tar.gz"
    art.write_bytes(b"release-payload-xyz")
    assert sign_release.main(["sign", "0.3.0", str(art), "--key", str(priv_file)]) == 0

    import json
    manifest = json.loads((tmp_path / "experanto-edge-0.3.0.json").read_text())
    ok, why = update.verify_manifest(manifest, "0.3.0", str(art), pub_b64)
    assert ok, why


# --- --selfcheck probe (pre-swap health-check used by the OTA helper) ---

def test_selfcheck_returns_zero(tmp_path, capsys):
    from experanto_edge.main import main
    cfg = tmp_path / "config.yaml"
    cfg.write_text("device_code: EXP-TEST\nsecret: s\nbroker_host: localhost\ntls: false\n")
    rc = main(["--selfcheck", "--config", str(cfg)])
    assert rc == 0
    assert "selfcheck ok" in capsys.readouterr().out
