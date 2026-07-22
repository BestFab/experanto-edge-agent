"""Esito/retry dei publish critici (ack, chunk history) + parita' di firma del
contratto Transport.

La telemetria ha il buffer store-and-forward; ack e chunk history NO: fino a
0.3.3 il loro esito publish era ignorato (ack) o solo contato (history), quindi
un chunk perso produceva una curva parziale SILENZIOSA lato server. Ora passano
da Agent._publish_critical (retry + esito reale) e fetch_history acka ok=False
se anche dopo i retry mancano chunk.
"""
import inspect

from experanto_edge.buffer import Buffer
from experanto_edge.config import Config
from experanto_edge.main import Agent
from experanto_edge.transport import MqttTransport, Transport, TransportError


class FakeReader:
    reader_type = "fake"

    def read(self):
        return {"read_at": 111, "getjp": {"801_170": {"ok": 1}}}

    def discover(self):
        return {"getjp": {}}


class HistReader(FakeReader):
    def fetch_history_curves(self, daysback):
        return {"ch860": {"3": "EPOCH"}, "curves": {"0": "C0", "1": "C1"}}


class FlakyTransport:
    """Fallisce i primi `fail_first` publish sui topic che finiscono con uno dei
    `flaky_suffixes` (contatore per-topic), poi riesce. `raise_error=True` alza
    TransportError invece di ritornare False (publish su client caduto)."""

    def __init__(self, cmd=None, fail_first=0, flaky_suffixes=(), raise_error=False):
        self.published = []
        self.attempts = {}
        self.cmd = cmd
        self.fail_first = fail_first
        self.flaky_suffixes = tuple(flaky_suffixes)
        self.raise_error = raise_error

    def connect(self, persistent=False):
        pass

    def disconnect(self):
        pass

    def connected(self):
        return True

    def subscribe(self, topic):
        pass

    def publish(self, topic, payload, qos=1, retain=False):
        if topic.endswith(self.flaky_suffixes):
            n = self.attempts[topic] = self.attempts.get(topic, 0) + 1
            if n <= self.fail_first:
                if self.raise_error:
                    raise TransportError("client caduto")
                return False
        self.published.append((topic, payload))
        return True

    def get_retained_command(self, topic, timeout=3.0):
        c, self.cmd = self.cmd, None
        return c

    def next_command(self, timeout):
        c, self.cmd = self.cmd, None
        return c


def make_agent(tmp_path, transport, reader=None):
    cfg = Config(device_code="EXP-T", secret="s", station_id="st-1", interval=300,
                 buffer_path=str(tmp_path / "buf.db"))
    cfg._path = str(tmp_path / "config.yaml")
    agent = Agent(cfg, reader or FakeReader(), transport, Buffer(cfg.buffer_path))
    agent.PUBLISH_RETRY_DELAY = 0     # niente attese reali nei test
    return agent, cfg


# ---------------- contratto Transport ----------------

def test_transport_connect_signature_has_persistent():
    # L'astratto deve dichiarare lo stesso parametro dell'implementazione reale:
    # fino a 0.3.3 connect() astratto non aveva `persistent` (asimmetria di firma).
    for klass in (Transport, MqttTransport):
        params = inspect.signature(klass.connect).parameters
        assert "persistent" in params, klass.__name__
        assert params["persistent"].default is False, klass.__name__


# ---------------- retry ack ----------------

def test_ack_retries_until_success(tmp_path):
    t = FlakyTransport(cmd={"command_id": "c1", "cmd": "get_diag", "args": {}},
                       fail_first=2, flaky_suffixes=("up/ack",))
    agent, cfg = make_agent(tmp_path, t)
    agent.run_cycle()
    acks = [p for p in t.published if p[0] == cfg.topic("up/ack")]
    assert len(acks) == 1                              # consegnato al 3o tentativo
    assert t.attempts[cfg.topic("up/ack")] == 3

def test_ack_transport_error_does_not_crash_cycle(tmp_path):
    # publish che ALZA (client caduto) durante l'ack: il ciclo deve sopravvivere
    # (prima l'eccezione risaliva fino a run_forever).
    t = FlakyTransport(cmd={"command_id": "c2", "cmd": "get_diag", "args": {}},
                       fail_first=99, flaky_suffixes=("up/ack",), raise_error=True)
    agent, cfg = make_agent(tmp_path, t)
    agent.run_cycle()                                  # non deve alzare
    assert cfg.last_command_id == "c2"                 # comando comunque marcato gestito

def test_ack_persistent_retries(tmp_path):
    t = FlakyTransport(fail_first=1, flaky_suffixes=("up/ack",))
    agent, cfg = make_agent(tmp_path, t)
    agent._dispatch_persistent({"command_id": "p1", "cmd": "get_diag", "args": {}})
    assert [p for p in t.published if p[0] == cfg.topic("up/ack")]
    assert t.attempts[cfg.topic("up/ack")] == 2


# ---------------- retry chunk history ----------------

def _hist(t, cfg):
    return [p for p in t.published if p[0] == cfg.topic("up/history")]

def test_history_chunk_retries_then_succeeds(tmp_path):
    t = FlakyTransport(fail_first=1, flaky_suffixes=("up/history",))
    agent, cfg = make_agent(tmp_path, t, HistReader())
    ok, detail = agent.fetch_history({"date": "2026-07-22", "daysback": 1},
                                     {"command_id": "h1"})
    assert ok
    assert len(_hist(t, cfg)) == 2
    assert '"sent": 2' in detail

def test_history_lost_chunks_reported_in_ack(tmp_path):
    # Chunk persi anche dopo i retry -> ok=False e done=False nel detail: il
    # server NON deve trattare la curva parziale come completa (perdita silenziosa).
    t = FlakyTransport(fail_first=99, flaky_suffixes=("up/history",))
    agent, cfg = make_agent(tmp_path, t, HistReader())
    ok, detail = agent.fetch_history({"date": "2026-07-22", "daysback": 1},
                                     {"command_id": "h2"})
    assert not ok
    assert '"done": false' in detail
    assert '"sent": 0' in detail and '"n_devices": 2' in detail
    # ogni chunk e' stato ritentato PUBLISH_ATTEMPTS volte
    assert t.attempts[cfg.topic("up/history")] == 2 * Agent.PUBLISH_ATTEMPTS
