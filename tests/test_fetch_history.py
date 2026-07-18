"""Storico on-demand lato Pi (Stage 2): reader curva 143:100, chunking up/history,
loop a connessione persistente.

- SolarlogGetjpReader.fetch_history_curves: forward-raw della curva del giorno + 860,
  con riuso delle cache calde (indici/860) per stare nel budget ~22s del server.
- Agent.fetch_history: un chunk up/history per device, correlati da command_id.
- _run_persistent / _publish_persistent: telemetria sul timer, comandi istantanei,
  buffer quando il broker e' giu'. Il path intermittente (default) resta invariato.
"""
from experanto_edge.buffer import Buffer
from experanto_edge.config import Config
from experanto_edge.main import Agent
from experanto_edge.readers.base import ReaderError
from experanto_edge.readers.solarlog_getjp import SolarlogGetjpReader

DEV = {"782": {"0": {"101": 6000}, "1": {"101": 6345}}}
SERIALS = {"740": {"0": "1 / SN-A", "1": "2 / SN-B"}}


def _info(name, serial, model):
    a = [0] * 21
    a[1], a[13], a[19] = name, serial, model
    return a


_CM = [[1, 0], [6, 0]]   # Pac col0, Temp col1 (l'indice E' la colonna del 143)


def _epoch(i):
    dev0 = [_info("Inv 1", "SN-A", "MAX-125KTL3-XLV"), _CM, [[7, 0]]]
    dev1 = [_info("Inv 2", "SN-B", "MAX-125KTL3-XLV"), _CM, [[7, 0]]]
    return [[i, 2], [[0, "1.1.2001", 1, "now", 300], [dev0, dev1]]]


CURVE0 = [[0, 0, 300], [["12:00:00", [5000, 45]]]]
CURVE1 = [[0, 0, 300], [["12:00:00", [6000, 46]]]]


class FakeResp:
    def __init__(self, payload=None, status=200):
        self._p, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError("http error")

    def json(self):
        return self._p


# ---------------- reader: fetch_history_curves ----------------

def test_fetch_history_curves_cold_forwards_raw(monkeypatch):
    def fake_post(url, json=None, timeout=None, headers=None):
        if json == {"782": None}:
            return FakeResp(DEV)
        if json == {"740": None}:
            return FakeResp(SERIALS)
        if isinstance(json, dict) and "860" in json:
            idx = list(json["860"].keys())[0]
            return FakeResp({"860": {idx: _epoch(int(idx))}}) if idx in ("0", "1") \
                else FakeResp({"860": {}})
        if isinstance(json, dict) and "143" in json:
            dev = list(json["143"].keys())[0]
            node = CURVE0 if dev == "0" else CURVE1
            return FakeResp({"143": {dev: {"100": {"1": node}}}})
        raise AssertionError(f"query inattesa {json}")

    monkeypatch.setattr("experanto_edge.readers.solarlog_getjp.requests.post", fake_post)
    out = SolarlogGetjpReader("192.168.1.50", spacing=0).fetch_history_curves(daysback=1)
    assert set(out["curves"].keys()) == {"0", "1"}
    assert out["curves"]["0"] == CURVE0
    assert out["curves"]["1"] == CURVE1
    assert "1" in out["ch860"]           # epoch corrente = indice piu' alto valido
    # gli indici sono ora scaldati per le chiamate successive
    assert set(out["curves"].keys()) == {"0", "1"}


def test_fetch_history_curves_reuses_warm_caches(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None, headers=None):
        calls.append(json)
        if isinstance(json, dict) and "143" in json:
            dev = list(json["143"].keys())[0]
            return FakeResp({"143": {dev: {"100": {"0": CURVE0 if dev == "0" else CURVE1}}}})
        raise AssertionError(f"cache calda: nessun 782/740/860 atteso, ricevuto {json}")

    monkeypatch.setattr("experanto_edge.readers.solarlog_getjp.requests.post", fake_post)
    r = SolarlogGetjpReader("192.168.1.50", spacing=0)
    r._last_indices = ["0", "1"]              # scaldati dal ciclo telemetria
    r._channels_860 = {"3": _epoch(3)}
    out = r.fetch_history_curves(daysback=0)
    assert set(out["curves"].keys()) == {"0", "1"}
    assert out["ch860"] == {"3": _epoch(3)}    # riusato, non rifetchato
    assert all(isinstance(c, dict) and "143" in c for c in calls)


# ---------------- agent: fetch_history chunking ----------------

class FakeReader:
    reader_type = "fake"

    def read(self):
        return {"read_at": 111, "getjp": {"801_170": {"ok": 1}}}

    def discover(self):
        return {"getjp": {}}


class HistReader(FakeReader):
    def fetch_history_curves(self, daysback):
        return {"ch860": {"3": "EPOCH"}, "curves": {"0": "C0", "1": "C1"}}


class FakeTransport:
    def __init__(self, cmd=None):
        self.published = []
        self.cmd = cmd
        self.connected_flag = False

    def connect(self, persistent=False):
        self.connected_flag = True

    def disconnect(self):
        self.connected_flag = False

    def connected(self):
        return self.connected_flag

    def subscribe(self, topic):
        self.published.append(("_sub", topic))

    def publish(self, topic, payload, qos=1, retain=False):
        self.published.append((topic, payload))
        return True

    def get_retained_command(self, topic, timeout=3.0):
        c, self.cmd = self.cmd, None
        return c

    def next_command(self, timeout):
        c, self.cmd = self.cmd, None
        return c


def make_cfg(tmp_path):
    cfg = Config(device_code="EXP-T", secret="s", station_id="st-1", interval=300,
                 buffer_path=str(tmp_path / "buf.db"))
    cfg._path = str(tmp_path / "config.yaml")
    return cfg


def _hist(t, cfg):
    return [p for p in t.published if p[0] == cfg.topic("up/history")]


def test_fetch_history_publishes_per_device_chunks(tmp_path):
    cfg = make_cfg(tmp_path)
    t = FakeTransport()
    agent = Agent(cfg, HistReader(), t, Buffer(cfg.buffer_path))
    ok, detail = agent.fetch_history({"date": "2026-07-16", "daysback": 2},
                                     {"command_id": "cmd-1"})
    assert ok
    hist = _hist(t, cfg)
    assert len(hist) == 2                                  # un chunk per device
    assert sorted(p[1]["device_idx"] for p in hist) == [0, 1]
    for _topic, payload in hist:
        assert payload["command_id"] == "cmd-1"
        assert payload["total_devices"] == 2
        assert payload["date"] == "2026-07-16"
        assert "860" in payload["raw"] and "143" in payload["raw"]   # 860 in ogni chunk
    p0 = [p for _, p in hist if p["device_idx"] == 0][0]
    assert p0["raw"]["143"]["0"]["100"]["2"] == "C0"       # annidato per daysback
    assert p0["raw"]["860"] == {"3": "EPOCH"}


def test_fetch_history_unsupported_reader(tmp_path):
    cfg = make_cfg(tmp_path)
    agent = Agent(cfg, FakeReader(), FakeTransport(), Buffer(cfg.buffer_path))
    ok, _ = agent.fetch_history({"daysback": 1}, {})
    assert not ok                                          # reader senza storico -> onesto no


def test_fetch_history_bad_daysback(tmp_path):
    cfg = make_cfg(tmp_path)
    agent = Agent(cfg, HistReader(), FakeTransport(), Buffer(cfg.buffer_path))
    assert not agent.fetch_history({"date": "x"}, {})[0]           # daysback mancante
    assert not agent.fetch_history({"daysback": -1}, {})[0]        # negativo
    assert not _hist(FakeTransport(), cfg)                          # niente pubblicato


def test_dispatch_fetch_history_end_to_end(tmp_path):
    # Catena completa nel ciclo intermittente: retained cmd -> dispatcher -> agent -> chunk + ack.
    cfg = make_cfg(tmp_path)
    t = FakeTransport(cmd={"command_id": "h1", "cmd": "fetch_history",
                           "args": {"date": "2026-07-16", "daysback": 1}})
    agent = Agent(cfg, HistReader(), t, Buffer(cfg.buffer_path))
    agent.run_cycle()
    assert len(_hist(t, cfg)) == 2
    acks = [p for p in t.published if p[0] == cfg.topic("up/ack")]
    assert acks and acks[0][1]["ok"] is True


# ---------------- persistent loop ----------------

def test_persistent_publishes_and_dispatches(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.persistent_commands = True
    t = FakeTransport(cmd={"command_id": "c1", "cmd": "set_interval", "args": {"interval": 600}})
    agent = Agent(cfg, FakeReader(), t, Buffer(cfg.buffer_path))
    # ferma il loop appena il primo comando e' stato processato
    orig = agent._dispatch_persistent

    def stop_after(cmd):
        orig(cmd)
        agent._stop.set()
    agent._dispatch_persistent = stop_after
    agent._run_persistent()
    assert ("_sub", cfg.topic("dn/cmd")) in t.published    # sottoscritto dn/cmd
    assert cfg.topic("up/telemetry") in [p[0] for p in t.published]
    assert cfg.interval == 600                             # comando eseguito
    assert not t.connected_flag                            # disconnesso in uscita


def test_publish_persistent_buffers_when_down(tmp_path):
    cfg = make_cfg(tmp_path)
    t = FakeTransport()          # connected_flag=False (mai connesso)
    buf = Buffer(cfg.buffer_path)
    Agent(cfg, FakeReader(), t, buf)._publish_persistent()
    assert buf.count() == 1                                # telemetria bufferizzata
    assert not t.published                                 # niente pubblicato mentre giu'


def test_reader_error_persistent_still_status(tmp_path):
    cfg = make_cfg(tmp_path)
    t = FakeTransport()
    t.connect(persistent=True)

    class Boom(FakeReader):
        def read(self):
            raise ReaderError("boom")
    Agent(cfg, Boom(), t, Buffer(cfg.buffer_path))._publish_persistent()
    statuses = [p for p in t.published if p[0] == cfg.topic("up/status")]
    assert statuses and statuses[0][1]["error"]
    assert cfg.topic("up/telemetry") not in [p[0] for p in t.published]
