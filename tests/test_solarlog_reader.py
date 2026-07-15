import pytest

from experanto_edge.readers.base import ReaderError
from experanto_edge.readers.solarlog_getjp import SolarlogGetjpReader

AGG = {"801": {"170": {"101": 12345, "102": 12800, "105": 42000, "116": 780000}}}
DEV = {"782": {"0": {"101": 6000}, "1": {"101": 6345}}}
STATUS = {"608": {"0": "Normal", "1": "OFFLINE"}}


class FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception("http error")

    def json(self):
        return self._p


def test_read_forwards_raw_getjp(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        if json == {"801": {"170": None}}:
            return FakeResp(AGG)
        if json == {"782": None}:
            return FakeResp(DEV)
        if json == {"608": None}:
            return FakeResp(STATUS)
        raise AssertionError(f"unexpected query {json}")

    monkeypatch.setattr(
        "experanto_edge.readers.solarlog_getjp.requests.post", fake_post
    )
    out = SolarlogGetjpReader("192.168.1.50", spacing=0).read()
    assert out["getjp"]["801_170"] == AGG
    assert out["getjp"]["782"] == DEV
    assert out["getjp"]["608"] == STATUS       # status per-inverter inoltrato
    assert isinstance(out["read_at"], int)


def test_608_is_best_effort(monkeypatch):
    # Se il blocco status 608 fallisce (503), la lettura di potenza NON si perde:
    # 608 = None, 801_170 e 782 restano validi.
    import requests

    def fake_post(url, json=None, timeout=None):
        if json == {"608": None}:
            raise requests.RequestException("503 Service Unavailable")
        return FakeResp(AGG if json == {"801": {"170": None}} else DEV)

    monkeypatch.setattr(
        "experanto_edge.readers.solarlog_getjp.requests.post", fake_post
    )
    out = SolarlogGetjpReader("192.168.1.50", spacing=0).read()
    assert out["getjp"]["801_170"] == AGG
    assert out["getjp"]["782"] == DEV
    assert out["getjp"]["608"] is None


def test_discover_returns_782(monkeypatch):
    monkeypatch.setattr(
        "experanto_edge.readers.solarlog_getjp.requests.post",
        lambda url, json=None, timeout=None: FakeResp(DEV),
    )
    out = SolarlogGetjpReader("192.168.1.50", spacing=0).discover()
    assert out["getjp"]["782"] == DEV


def test_read_error_wrapped(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.RequestException("down")

    monkeypatch.setattr("experanto_edge.readers.solarlog_getjp.requests.post", boom)
    with pytest.raises(ReaderError):
        SolarlogGetjpReader("192.168.1.50").read()


def test_no_ip_does_not_crash_at_init_but_read_raises():
    # Datalogger assente: NON crasha al costruttore (l'agente deve partire lo stesso),
    # ma read() solleva ReaderError -> run_cycle lo gestisce (niente crash-loop).
    r = SolarlogGetjpReader("")            # non solleva
    with pytest.raises(ReaderError):
        r.read()
