import pytest

from experanto_edge.readers.base import ReaderError
from experanto_edge.readers.solarlog_getjp import SolarlogGetjpReader

AGG = {"801": {"170": {"101": 12345, "102": 12800, "105": 42000, "116": 780000}}}
DEV = {"782": {"0": {"101": 6000}, "1": {"101": 6345}}}


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
        raise AssertionError(f"unexpected query {json}")

    monkeypatch.setattr(
        "experanto_edge.readers.solarlog_getjp.requests.post", fake_post
    )
    out = SolarlogGetjpReader("192.168.1.50").read()
    assert out["getjp"]["801_170"] == AGG
    assert out["getjp"]["782"] == DEV
    assert isinstance(out["read_at"], int)


def test_discover_returns_782(monkeypatch):
    monkeypatch.setattr(
        "experanto_edge.readers.solarlog_getjp.requests.post",
        lambda url, json=None, timeout=None: FakeResp(DEV),
    )
    out = SolarlogGetjpReader("192.168.1.50").discover()
    assert out["getjp"]["782"] == DEV


def test_read_error_wrapped(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.RequestException("down")

    monkeypatch.setattr("experanto_edge.readers.solarlog_getjp.requests.post", boom)
    with pytest.raises(ReaderError):
        SolarlogGetjpReader("192.168.1.50").read()


def test_requires_ip():
    with pytest.raises(ReaderError):
        SolarlogGetjpReader("")
