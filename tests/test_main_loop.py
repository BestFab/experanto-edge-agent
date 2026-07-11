from experanto_edge.buffer import Buffer
from experanto_edge.config import Config
from experanto_edge.main import Agent
from experanto_edge.readers.base import ReaderError


class FakeReader:
    reader_type = "fake"

    def __init__(self):
        self.fail = False

    def read(self):
        if self.fail:
            raise ReaderError("boom")
        return {"read_at": 111, "getjp": {"801_170": {"ok": 1}}}

    def discover(self):
        return {"getjp": {"782": {"0": {}}}}


class FakeTransport:
    def __init__(self, cmd=None, fail_connect=False, fail_publish=False):
        self.published = []
        self.cmd = cmd
        self.fail_connect = fail_connect
        self.fail_publish = fail_publish
        self.connected = False

    def connect(self):
        if self.fail_connect:
            from experanto_edge.transport import TransportError

            raise TransportError("no broker")
        self.connected = True

    def disconnect(self):
        self.connected = False

    def publish(self, topic, payload, qos=1, retain=False):
        if self.fail_publish and topic.endswith("up/telemetry"):
            return False
        self.published.append((topic, payload))
        return True

    def get_retained_command(self, topic, timeout=3.0):
        c, self.cmd = self.cmd, None
        return c


def make_cfg(tmp_path):
    cfg = Config(
        device_code="EXP-TEST",
        secret="s",
        station_id="st-1",
        interval=300,
        buffer_path=str(tmp_path / "buf.db"),
    )
    cfg._path = str(tmp_path / "config.yaml")
    return cfg


def topics(t):
    return [p[0] for p in t.published]


def test_cycle_publishes_telemetry_and_status(tmp_path):
    cfg = make_cfg(tmp_path)
    t = FakeTransport()
    Agent(cfg, FakeReader(), t, Buffer(cfg.buffer_path)).run_cycle()
    assert cfg.topic("up/telemetry") in topics(t)
    assert cfg.topic("up/status") in topics(t)


def test_command_dispatch_and_ack(tmp_path):
    cfg = make_cfg(tmp_path)
    t = FakeTransport(cmd={"command_id": "c1", "cmd": "set_interval", "args": {"interval": 600}})
    Agent(cfg, FakeReader(), t, Buffer(cfg.buffer_path)).run_cycle()
    assert cfg.interval == 600
    acks = [p for p in t.published if p[0] == cfg.topic("up/ack")]
    assert acks and acks[0][1]["ok"] is True
    assert cfg.last_command_id == "c1"


def test_command_dedup(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.last_command_id = "c1"
    t = FakeTransport(cmd={"command_id": "c1", "cmd": "read_now", "args": {}})
    Agent(cfg, FakeReader(), t, Buffer(cfg.buffer_path)).run_cycle()
    assert not [p for p in t.published if p[0] == cfg.topic("up/ack")]


def test_buffer_on_connect_failure(tmp_path):
    cfg = make_cfg(tmp_path)
    buf = Buffer(cfg.buffer_path)
    Agent(cfg, FakeReader(), FakeTransport(fail_connect=True), buf).run_cycle()
    assert buf.count() == 1


def test_buffer_flushes_next_cycle(tmp_path):
    cfg = make_cfg(tmp_path)
    buf = Buffer(cfg.buffer_path)
    agent = Agent(cfg, FakeReader(), FakeTransport(fail_connect=True), buf)
    agent.run_cycle()
    assert buf.count() == 1
    agent.transport = FakeTransport()
    agent.run_cycle()
    assert buf.count() == 0
    assert cfg.topic("up/telemetry") in topics(agent.transport)


def test_publish_failure_buffers(tmp_path):
    cfg = make_cfg(tmp_path)
    buf = Buffer(cfg.buffer_path)
    Agent(cfg, FakeReader(), FakeTransport(fail_publish=True), buf).run_cycle()
    assert buf.count() == 1


def test_reader_error_still_sends_status(tmp_path):
    cfg = make_cfg(tmp_path)
    r = FakeReader()
    r.fail = True
    t = FakeTransport()
    Agent(cfg, r, t, Buffer(cfg.buffer_path)).run_cycle()
    assert cfg.topic("up/status") in topics(t)
    assert cfg.topic("up/telemetry") not in topics(t)
    status = [p[1] for p in t.published if p[0] == cfg.topic("up/status")][0]
    assert status["error"]
