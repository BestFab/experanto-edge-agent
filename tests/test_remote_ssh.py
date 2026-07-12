"""On-demand remote SSH (Tailscale) — open_ssh/close_ssh + window enforcement.

No real tailscaled: remote._run (and, for the agent tests, remote.up/down) are stubbed,
so we assert the CLI args we build and that the agent tracks the SSH window in-process.
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


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_cfg(tmp_path, **kw):
    cfg = Config(device_code="EXP-TEST", secret="s", station_id="st-1",
                 interval=300, buffer_path=str(tmp_path / "buf.db"), **kw)
    cfg._path = str(tmp_path / "config.yaml")
    return cfg


def _ack(t, cfg):
    return [p[1] for p in t.published if p[0] == cfg.topic("up/ack")][0]


# --- remote.up / down: CLI arg building -------------------------------------

def test_up_builds_ssh_authkey_args(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, timeout):
        calls.append(args)
        if args[:2] == ["ip", "-4"]:
            return FakeProc(0, stdout="100.64.0.7\n")
        return FakeProc(0)

    monkeypatch.setattr(remote, "_run", fake_run)
    cfg = make_cfg(tmp_path, tailscale_hostname="pi-nord")
    ok, info = remote.up(cfg, "tskey-abc", 900)
    assert ok is True
    assert info["ip"] == "100.64.0.7"
    up = next(c for c in calls if c and c[0] == "up")
    assert "--ssh" in up and "tskey-abc" in up and "pi-nord" in up


def test_up_without_key_fails(tmp_path):
    ok, why = remote.up(make_cfg(tmp_path), "", 900)
    assert ok is False and "authkey" in why


def test_up_login_server_for_headscale(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(remote, "_run",
                        lambda args, timeout: (calls.append(args) or FakeProc(0, stdout="100.0.0.1\n")))
    cfg = make_cfg(tmp_path, tailscale_login_server="https://hs.example.com")
    ok, _ = remote.up(cfg, "tskey-x", 600)
    up = next(c for c in calls if c and c[0] == "up")
    assert "--login-server=https://hs.example.com" in up


def test_up_nonzero_returncode_is_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, "_run", lambda args, timeout: FakeProc(1, stderr="boom"))
    ok, why = remote.up(make_cfg(tmp_path), "tskey-x", 600)
    assert ok is False and "boom" in why


# --- Agent.open_ssh / close_ssh + dispatch + enforcement --------------------

def _stub_remote(monkeypatch, ip="100.64.0.7"):
    ups, downs = [], []
    monkeypatch.setattr(remote, "up",
                        lambda cfg, authkey, ttl: (ups.append((authkey, ttl)) or (True, {"ip": ip, "host": "EXP-TEST"})))
    monkeypatch.setattr(remote, "down",
                        lambda cfg=None: (downs.append(1) or (True, "")))
    return ups, downs


def test_open_ssh_sets_window_and_acks_overlay_ip(tmp_path, monkeypatch):
    ups, _ = _stub_remote(monkeypatch)
    cfg = make_cfg(tmp_path, tailscale_authkey="tskey-stored")
    t = FakeTransport(cmd={"command_id": "c1", "cmd": "open_ssh", "args": {"ttl": 600}})
    Agent(cfg, FakeReader(), t, Buffer(cfg.buffer_path)).run_cycle()
    assert cfg.ssh_open_until > 0
    assert ups and ups[0] == ("tskey-stored", 600)
    ack = _ack(t, cfg)
    assert ack["ok"] is True
    detail = json.loads(ack["detail"])
    assert detail["ts_ip"] == "100.64.0.7" and detail["until"] == cfg.ssh_open_until


def test_open_ssh_prefers_command_authkey(tmp_path, monkeypatch):
    ups, _ = _stub_remote(monkeypatch)
    cfg = make_cfg(tmp_path, tailscale_authkey="tskey-stored")
    t = FakeTransport(cmd={"command_id": "c2", "cmd": "open_ssh", "args": {"authkey": "tskey-ephemeral"}})
    Agent(cfg, FakeReader(), t, Buffer(cfg.buffer_path)).run_cycle()
    assert ups[0][0] == "tskey-ephemeral"


def test_close_ssh_clears_window(tmp_path, monkeypatch):
    _, downs = _stub_remote(monkeypatch)
    cfg = make_cfg(tmp_path)
    cfg.ssh_open_until = 9999999999
    t = FakeTransport(cmd={"command_id": "c3", "cmd": "close_ssh", "args": {}})
    Agent(cfg, FakeReader(), t, Buffer(cfg.buffer_path)).run_cycle()
    assert cfg.ssh_open_until == 0 and downs


def test_expired_window_auto_closes_on_cycle(tmp_path, monkeypatch):
    _, downs = _stub_remote(monkeypatch)
    cfg = make_cfg(tmp_path)
    cfg.ssh_open_until = 1  # in the past
    Agent(cfg, FakeReader(), FakeTransport(), Buffer(cfg.buffer_path)).run_cycle()
    assert cfg.ssh_open_until == 0 and downs


def test_status_reports_ssh_open_until(tmp_path, monkeypatch):
    _stub_remote(monkeypatch)
    cfg = make_cfg(tmp_path)
    cfg.ssh_open_until = 4102444800  # year 2100: enforcement won't clear it
    t = FakeTransport()
    Agent(cfg, FakeReader(), t, Buffer(cfg.buffer_path)).run_cycle()
    status = [p[1] for p in t.published if p[0] == cfg.topic("up/status")][0]
    assert status["ssh_open_until"] == 4102444800


def test_open_ssh_failure_leaves_window_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, "up", lambda cfg, authkey, ttl: (False, "tailscale up rc=1: boom"))
    cfg = make_cfg(tmp_path, tailscale_authkey="tskey-x")
    t = FakeTransport(cmd={"command_id": "c9", "cmd": "open_ssh", "args": {}})
    Agent(cfg, FakeReader(), t, Buffer(cfg.buffer_path)).run_cycle()
    assert cfg.ssh_open_until == 0
    assert _ack(t, cfg)["ok"] is False
