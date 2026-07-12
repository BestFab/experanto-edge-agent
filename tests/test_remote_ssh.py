"""On-demand remote SSH via reverse tunnel to a self-hosted bastion.

No real ssh/bastion: remote._spawn/_terminate are stubbed, so we assert the ssh -R command
we build, the pidfile lifecycle, and that the agent tracks the SSH window in-process.
"""
import json
import subprocess

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


class _Bytes:
    def __init__(self, b):
        self._b = b

    def read(self, *a):
        return self._b


class FakeProc:
    """alive=True → wait() raises TimeoutExpired (tunnel su); alive=False → exits with rc."""
    def __init__(self, pid=777, alive=True, rc=255, stderr=b""):
        self.pid = pid
        self._alive = alive
        self._rc = rc
        self.returncode = None if alive else rc
        self.stderr = _Bytes(stderr)

    def wait(self, timeout=None):
        if self._alive:
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=timeout)
        self.returncode = self._rc
        return self._rc


def make_cfg(tmp_path, **kw):
    cfg = Config(device_code="EXP-TEST", secret="s", station_id="st-1",
                 buffer_path=str(tmp_path / "buf.db"), **kw)
    cfg._path = str(tmp_path / "config.yaml")
    return cfg


def bastion_cfg(tmp_path, **kw):
    key = tmp_path / "id"
    key.write_text("PRIVATE-KEY")
    return make_cfg(tmp_path, ssh_bastion_host="vps.example.com", ssh_reverse_port=22016,
                    ssh_bastion_user="edge-tunnel", ssh_identity=str(key), **kw)


def _ack(t, cfg):
    return [p[1] for p in t.published if p[0] == cfg.topic("up/ack")][0]


def _stub(monkeypatch, proc=None):
    monkeypatch.setattr(remote, "_spawn", lambda cmd: proc or FakeProc())
    kills = []
    monkeypatch.setattr(remote, "_terminate", lambda pid: kills.append(pid))
    return kills


# --- _validate / _tunnel_cmd -------------------------------------------------

def test_validate_missing_bastion(tmp_path):
    assert "bastion" in remote._validate(make_cfg(tmp_path))


def test_validate_missing_reverse_port(tmp_path):
    key = tmp_path / "id"; key.write_text("k")
    cfg = make_cfg(tmp_path, ssh_bastion_host="h", ssh_identity=str(key))
    assert "reverse_port" in remote._validate(cfg)


def test_validate_missing_key(tmp_path):
    cfg = make_cfg(tmp_path, ssh_bastion_host="h", ssh_reverse_port=1, ssh_identity="/no/such/key")
    assert "chiave" in remote._validate(cfg)


def test_tunnel_cmd_builds_reverse_forward(tmp_path):
    cmd = remote._tunnel_cmd(bastion_cfg(tmp_path))
    assert "-R" in cmd and "22016:localhost:22" in cmd
    assert "edge-tunnel@vps.example.com" in cmd
    assert "-N" in cmd and "ExitOnForwardFailure=yes" in cmd


# --- up / down ---------------------------------------------------------------

def test_up_success_writes_pid_and_reach(tmp_path, monkeypatch):
    _stub(monkeypatch, FakeProc(pid=555, alive=True))
    cfg = bastion_cfg(tmp_path)
    ok, info = remote.up(cfg, 900)
    assert ok is True
    assert info["port"] == 22016 and "22016" in info["reach"]
    assert remote._read_pid(cfg) == 555


def test_up_fails_if_tunnel_exits_immediately(tmp_path, monkeypatch):
    _stub(monkeypatch, FakeProc(alive=False, rc=255, stderr=b"Permission denied (publickey)"))
    ok, why = remote.up(bastion_cfg(tmp_path), 900)
    assert ok is False and "255" in why and "Permission denied" in why


def test_up_validation_error_does_not_spawn(tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(remote, "_spawn", lambda cmd: spawned.append(cmd))
    ok, why = remote.up(make_cfg(tmp_path), 900)
    assert ok is False and not spawned


def test_down_terminates_and_clears_pid(tmp_path, monkeypatch):
    cfg = bastion_cfg(tmp_path)
    remote._write_pid(cfg, 999)
    kills = []
    monkeypatch.setattr(remote, "_terminate", lambda pid: kills.append(pid))
    ok, _ = remote.down(cfg)
    assert ok and kills == [999] and remote._read_pid(cfg) is None


# --- Agent integration -------------------------------------------------------

def test_open_ssh_sets_window_and_acks_reach(tmp_path, monkeypatch):
    _stub(monkeypatch)
    cfg = bastion_cfg(tmp_path)
    t = FakeTransport(cmd={"command_id": "c1", "cmd": "open_ssh", "args": {"ttl": 600}})
    Agent(cfg, FakeReader(), t, Buffer(cfg.buffer_path)).run_cycle()
    assert cfg.ssh_open_until > 0
    ack = _ack(t, cfg)
    assert ack["ok"] is True
    d = json.loads(ack["detail"])
    assert d["port"] == 22016 and d["until"] == cfg.ssh_open_until and d["bastion"] == "vps.example.com"


def test_close_ssh_clears_window(tmp_path, monkeypatch):
    kills = _stub(monkeypatch)
    cfg = bastion_cfg(tmp_path)
    cfg.ssh_open_until = 9999999999
    remote._write_pid(cfg, 321)
    t = FakeTransport(cmd={"command_id": "c3", "cmd": "close_ssh", "args": {}})
    Agent(cfg, FakeReader(), t, Buffer(cfg.buffer_path)).run_cycle()
    assert cfg.ssh_open_until == 0 and 321 in kills


def test_expired_window_auto_closes(tmp_path, monkeypatch):
    kills = _stub(monkeypatch)
    cfg = bastion_cfg(tmp_path)
    cfg.ssh_open_until = 1               # nel passato
    remote._write_pid(cfg, 42)
    Agent(cfg, FakeReader(), FakeTransport(), Buffer(cfg.buffer_path)).run_cycle()
    assert cfg.ssh_open_until == 0 and 42 in kills


def test_open_ssh_failure_leaves_window_closed(tmp_path, monkeypatch):
    _stub(monkeypatch, FakeProc(alive=False, rc=255, stderr=b"boom"))
    cfg = bastion_cfg(tmp_path)
    t = FakeTransport(cmd={"command_id": "c9", "cmd": "open_ssh", "args": {}})
    Agent(cfg, FakeReader(), t, Buffer(cfg.buffer_path)).run_cycle()
    assert cfg.ssh_open_until == 0
    assert _ack(t, cfg)["ok"] is False


def test_status_reports_ssh_open_until(tmp_path, monkeypatch):
    _stub(monkeypatch)
    cfg = bastion_cfg(tmp_path)
    cfg.ssh_open_until = 4102444800      # anno 2100: l'enforcement non lo chiude
    t = FakeTransport()
    Agent(cfg, FakeReader(), t, Buffer(cfg.buffer_path)).run_cycle()
    status = [p[1] for p in t.published if p[0] == cfg.topic("up/status")][0]
    assert status["ssh_open_until"] == 4102444800
