"""End-to-end (against a getjp mock): SolarlogGetjpReader -> Agent.run_cycle -> transport.
Anchors the Pi->server telemetry envelope that the server-side SolarlogLocalMonitor (E3)
will consume."""
from experanto_edge.buffer import Buffer
from experanto_edge.config import Config
from experanto_edge.main import Agent
from experanto_edge.readers.solarlog_getjp import SolarlogGetjpReader

AGG = {"801": {"170": {"101": 12345, "116": 780000}}}
DEV = {"782": {"0": {"101": 6000}, "1": {"101": 6345}}}


class FakeResp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class CollectTransport:
    def __init__(self):
        self.published = []

    def connect(self):
        pass

    def disconnect(self):
        pass

    def publish(self, topic, payload, qos=1, retain=False):
        self.published.append((topic, payload))
        return True

    def get_retained_command(self, topic, timeout=3.0):
        return None


def test_getjp_to_telemetry_envelope(tmp_path, monkeypatch):
    def fake_post(url, json=None, timeout=None):
        return FakeResp(AGG if json == {"801": {"170": None}} else DEV)

    monkeypatch.setattr("experanto_edge.readers.solarlog_getjp.requests.post", fake_post)

    cfg = Config(
        device_code="EXP-INT", secret="s", station_id="st-int",
        buffer_path=str(tmp_path / "b.db"),
    )
    cfg._path = str(tmp_path / "c.yaml")
    t = CollectTransport()
    Agent(cfg, SolarlogGetjpReader("10.0.0.5"), t, Buffer(cfg.buffer_path)).run_cycle()

    tele = [p[1] for p in t.published if p[0] == cfg.topic("up/telemetry")][0]
    assert tele["schema"] == "experanto.edge.telemetry/1"
    assert tele["station_id"] == "st-int"
    assert tele["reader_type"] == "solarlog_getjp"
    assert tele["data"]["getjp"]["801_170"] == AGG
    assert tele["data"]["getjp"]["782"] == DEV
