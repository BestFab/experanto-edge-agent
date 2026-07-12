"""On-demand remote access via WireGuard to a self-hosted hub.

No real WireGuard: remote._run (wg-quick) and remote._validate are stubbed, so we assert
the wg-quick invocations, the reach we build, and that the agent tracks the SSH window.
"""
import json

from experanto_edge import remote
from experanto_edge.buffer import Buffer
from experanto_edge.config import Config
from experanto_edge.main import Agent


class FakeReader:
    reader_type = "fake"

    def read(self):
        return {"read_at": 111, "getjp": {"801_170": {"ok": 1}}}

    def discover(self):
        return {}


class FakeTransport:
    def __init__(self, cmd=None):
        self.published = []
        self.cmd = cmd

    def connect(self):
        pass

    def disconnect(self):
        pass

    def publish(self, topic, payload, qos=1, retain=False):
        self.published.append((topic, payload))
        return True

    def get_retained_command(self, topic, timeout=3.0):
        c, self.cmd = self.cmd, None
        return c


class FakeCP:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_cfg(tmp_path, **kw):
    cfg = Config(device_code="EXP-TEST", secret="s", station_id="st-1",
                 buffer_path=str(tmp_path / "buf.db"), **kw)
    cfg._path = str(tmp_path / "config.yaml")
    return cfg


def wg_cfg(tmp_path, **kw):
    return make_cfg(tmp_path, wg_interface="wg-experanto", wg_address="10.8.0.5/32",
                    wg_ssh_user="fabri", **kw)


def _ack(t, cfg):
    return [p[1] for p in t.published if p[0] == cfg.topic("up/ack")][0]


def _ok_env(monkeypatch, run=None):
    """wg-quick present + config valid; capture the wg-quick argv lists."""
    monkeypatch.setattr(remote, "_validate", lambda cfg: "")
    calls = []

    def default_run(args, timeout=25):
        calls.append(args)
        return FakeCP(0)

    monkeypatch.setattr(remote, "_run", run or default_run)
    return calls


# --- _validate ---------------------------------------------------------------

def test_validate_no_wgquick(tmp_path, monkeypatch):
    monkeypatch.setattr(remote.shutil, "which", lambda x: None)
    assert "wg-quick" in remote._validate(wg_cfg(tmp_path))


def test_validate_no_conf(tmp_path, monkeypatch):
    monkeypatch.setattr(remote.shutil, "which", lambda x: "/usr/bin/wg-quick")
    monkeypatch.setattr(remote.os.path, "exists", lambda p: False)
    assert "assente" in remote._validate(wg_cfg(tmp_path))


def test_validate_no_address(tmp_path, monkeypatch):
    monkeypatch.setattr(remote.shutil, "which", lambda x: "/usr/bin/wg-quick")
    monkeypatch.setattr(remote.os.path, "exists", lambda p: True)
    assert "wg_address" in remote._validate(make_cfg(tmp_path))


# --- up / down ---------------------------------------------------------------

def test_up_brings_iface_and_returns_reach(tmp_path, monkeypatch):
    calls = _ok_env(monkeypatch)
    ok, info = remote.up(wg_cfg(tmp_path), 900)
    assert ok is True
    assert info["address"] == "10.8.0.5" and info["reach"] == "ssh fabri@10.8.0.5"
    assert any(a[:3] == ["sudo", "-n", "wg-quick"] and a[3] == "up" for a in calls)


def test_up_fails_when_wgquick_up_errors(tmp_path, monkeypatch):
    def run(args, timeout=25):
        return FakeCP(0) if args[3] == "down" else FakeCP(1, stderr="Address already in use")
    _ok_env(monkeypatch, run=run)
    ok, why = remote.up(wg_cfg(tmp_path), 900)
    assert ok is False and "Address already in use" in why


def test_up_validation_error_does_not_run(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, "_validate", lambda cfg: "wg_address non configurato")
    ran = []
    monkeypatch.setattr(remote, "_run", lambda *a, **k: ran.append(a))
    ok, why = remote.up(wg_cfg(tmp_path), 900)
    assert ok is False and not ran


def test_down_calls_wgquick_down(tmp_path, monkeypatch):
    calls = _ok_env(monkeypatch)
    remote.down(wg_cfg(tmp_path))
    assert any(a[3] == "down" for a in calls)


# --- Agent integration -------------------------------------------------------

def test_open_ssh_sets_window_and_acks_reach(tmp_path, monkeypatch):
    _ok_env(monkeypatch)
    cfg = wg_cfg(tmp_path)
    t = FakeTransport(cmd={"command_id": "c1", "cmd": "open_ssh", "args": {"ttl": 600}})
    Agent(cfg, FakeReader(), t, Buffer(cfg.buffer_path)).run_cycle()
    assert cfg.ssh_open_until > 0
    ack = _ack(t, cfg)
    assert ack["ok"] is True
    d = json.loads(ack["detail"])
    assert d["address"] == "10.8.0.5" and d["reach"] == "ssh fabri@10.8.0.5" and d["until"] == cfg.ssh_open_until


def test_close_ssh_clears_window(tmp_path, monkeypatch):
    calls = _ok_env(monkeypatch)
    cfg = wg_cfg(tmp_path)
    cfg.ssh_open_until = 9999999999
    t = FakeTransport(cmd={"command_id": "c3", "cmd": "close_ssh", "args": {}})
    Agent(cfg, FakeReader(), t, Buffer(cfg.buffer_path)).run_cycle()
    assert cfg.ssh_open_until == 0 and any(a[3] == "down" for a in calls)


def test_expired_window_auto_closes(tmp_path, monkeypatch):
    calls = _ok_env(monkeypatch)
    cfg = wg_cfg(tmp_path)
    cfg.ssh_open_until = 1
    Agent(cfg, FakeReader(), FakeTransport(), Buffer(cfg.buffer_path)).run_cycle()
    assert cfg.ssh_open_until == 0 and any(a[3] == "down" for a in calls)


def test_open_ssh_failure_leaves_window_closed(tmp_path, monkeypatch):
    def run(args, timeout=25):
        return FakeCP(0) if args[3] == "down" else FakeCP(1, stderr="boom")
    _ok_env(monkeypatch, run=run)
    cfg = wg_cfg(tmp_path)
    t = FakeTransport(cmd={"command_id": "c9", "cmd": "open_ssh", "args": {}})
    Agent(cfg, FakeReader(), t, Buffer(cfg.buffer_path)).run_cycle()
    assert cfg.ssh_open_until == 0
    assert _ack(t, cfg)["ok"] is False


def test_status_reports_ssh_open_until(tmp_path, monkeypatch):
    _ok_env(monkeypatch)
    cfg = wg_cfg(tmp_path)
    cfg.ssh_open_until = 4102444800
    t = FakeTransport()
    Agent(cfg, FakeReader(), t, Buffer(cfg.buffer_path)).run_cycle()
    status = [p[1] for p in t.published if p[0] == cfg.topic("up/status")][0]
    assert status["ssh_open_until"] == 4102444800
