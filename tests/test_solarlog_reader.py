import pytest

from experanto_edge.readers.base import ReaderError
from experanto_edge.readers.solarlog_getjp import SolarlogGetjpReader

AGG = {"801": {"170": {"101": 12345, "102": 12800, "105": 42000, "116": 780000}}}
DEV = {"782": {"0": {"101": 6000}, "1": {"101": 6345}}}
STATUS = {"608": {"0": "Normal", "1": "OFFLINE"}}
SALTS = {"550": {"104": "$2b$08$qk5birSAh8VntKj4PkvTaO", "112": 1, "113": 1}}


def _info(name, serial, model):
    """info21 di un device 860: [1]=nome, [13]=seriale, [19]=modello."""
    a = [0] * 21
    a[1], a[13], a[19] = name, serial, model
    return a


# channels.min a 2 colonne: [Pac ch0, Temp ch0]. L'indice E' la colonna del 143.
_CM = [[1, 0], [6, 0]]


def _epoch(i):
    """860[i] = [[idx, ndev], [epoch_meta, [dev0, dev1]]]; dev = [info21, channels_min, ...]."""
    dev0 = [_info("Inv 1", "SN-A", "MAX-125KTL3-XLV"), _CM, [[7, 0]]]
    dev1 = [_info("Inv 2", "SN-B", "MAX-125KTL3-XLV"), _CM, [[7, 0]]]
    return [[i, 2], [[0, "1.1.2001", 1, "now", 300], [dev0, dev1]]]


# 143:101 = valori CORRENTI: [[from, to, interval], [65 valori]] (qui 2, come channels.min).
DETAIL_101_0 = {"143": {"1": {"101": {"0": [[1000, 2000, 300], [5000, 41]]}}}}
DETAIL_101_1 = {"143": {"1": {"101": {"1": [[1000, 2000, 300], [6000, 43]]}}}}


class FakeResp:
    def __init__(self, payload=None, status=200, text="SUCCESS"):
        self._p = payload
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError("http error")

    def json(self):
        return self._p


def _open_post(json=None):
    """Risposte del livello open (usato dai fake requests.post module-level)."""
    if json == {"801": {"170": None}}:
        return FakeResp(AGG)
    if json == {"782": None}:
        return FakeResp(DEV)
    if json == {"608": None}:
        return FakeResp(STATUS)
    if json == {"740": None}:
        return FakeResp({"740": {"0": "1 / SN-A", "1": "2 / SN-B"}})
    if json == {"877": None}:
        return FakeResp({"877": [["2026-07", 42000]]})
    if json == {"878": None}:
        return FakeResp({"878": [["2026", 500000]]})
    if json == {"550": None}:
        return FakeResp(SALTS)
    # dettaglio privilegiato senza login: un datalogger che lo protegge risponde negato
    if isinstance(json, dict) and ("143" in json or "860" in json):
        return FakeResp(status=403, text="ACCESS DENIED")
    return None


def _detail_post(json):
    """Risposte 860 (epoch 0,1 valide; 2+ vuota) + 143:101 per device. None se non gestita."""
    if isinstance(json, dict) and "860" in json:
        idx = list(json["860"].keys())[0]
        if idx in ("0", "1"):
            return FakeResp({"860": {idx: _epoch(int(idx))}})
        return FakeResp({"860": {}})              # nessuna altra epoch -> stop iterazione
    if isinstance(json, dict) and "143" in json:
        dev = list(json["143"]["1"]["101"].keys())[0]
        return FakeResp(DETAIL_101_0 if dev == "0" else DETAIL_101_1)
    return None


def test_read_forwards_raw_getjp(monkeypatch):
    def fake_post(url, json=None, timeout=None, headers=None):
        r = _open_post(json)
        if r is None:
            raise AssertionError(f"unexpected query {json}")
        return r

    monkeypatch.setattr(
        "experanto_edge.readers.solarlog_getjp.requests.post", fake_post
    )
    out = SolarlogGetjpReader("192.168.1.50", spacing=0).read()
    assert out["getjp"]["801_170"] == AGG
    assert out["getjp"]["782"] == DEV
    assert out["getjp"]["608"] == STATUS       # status per-inverter inoltrato
    assert "143" not in out["getjp"]           # datalogger nega il dettaglio -> assente
    assert "860" not in out["getjp"]
    assert isinstance(out["read_at"], int)


def test_608_is_best_effort(monkeypatch):
    # Se il blocco status 608 fallisce (503), la lettura di potenza NON si perde:
    # 608 = None, 801_170 e 782 restano validi.
    import requests

    def fake_post(url, json=None, timeout=None, headers=None):
        if json == {"608": None}:
            raise requests.RequestException("503 Service Unavailable")
        r = _open_post(json)
        return r if r is not None else FakeResp(DEV)

    monkeypatch.setattr(
        "experanto_edge.readers.solarlog_getjp.requests.post", fake_post
    )
    out = SolarlogGetjpReader("192.168.1.50", spacing=0).read()
    assert out["getjp"]["801_170"] == AGG
    assert out["getjp"]["782"] == DEV
    assert out["getjp"]["608"] is None


def test_detail_skipped_when_collect_detail_false(monkeypatch):
    # collect_detail=False (default dell'AGENTE via config): ne' 143 ne' 860 vengono
    # mai interrogati, ma i blocchi open restano.
    def fake_post(url, json=None, timeout=None, headers=None):
        if isinstance(json, dict) and ("143" in json or "860" in json):
            raise AssertionError("143/860 non devono essere interrogati con collect_detail=False")
        r = _open_post(json)
        return r if r is not None else FakeResp(DEV)

    monkeypatch.setattr("experanto_edge.readers.solarlog_getjp.requests.post", fake_post)
    out = SolarlogGetjpReader("192.168.1.50", spacing=0, user_password="x",
                              collect_detail=False).read()
    assert "143" not in out["getjp"]
    assert "860" not in out["getjp"]
    assert out["getjp"]["740"] == {"740": {"0": "1 / SN-A", "1": "2 / SN-B"}}
    assert out["getjp"]["877"] == {"877": [["2026-07", 42000]]}
    assert out["getjp"]["878"] == {"878": [["2026", 500000]]}


def test_real_inverter_indices_excludes_meter_and_empty():
    from experanto_edge.readers.solarlog_getjp import _real_inverter_indices
    serials = {"740": {"0": "1 / SN-A", "1": "2 / SN-B",
                       "9": "192.168.1.60 / 70124278", "10": "Err"}}
    # dal 740: solo inverter reali (contatore=IP a sx e slot "Err" esclusi).
    assert _real_inverter_indices(serials, None) == ["0", "1"]
    # senza 740: ripiega sugli slot 782 con potenza non-zero.
    assert set(_real_inverter_indices({}, {"0": "5000", "1": "0", "2": "3000"})) == {"0", "2"}


def test_detail_only_queries_real_inverters(monkeypatch):
    # Con 30+ slot nel 782 e 740 che marca 2 inverter reali, il 143 va interrogato
    # SOLO su quei 2 (non su tutti gli slot).
    queried = []
    flat782 = {str(i): "0" for i in range(30)}
    flat782["0"] = "5000"; flat782["1"] = "6000"; flat782["9"] = "999999"
    serials740 = {"740": {"0": "1 / SN-A", "1": "2 / SN-B",
                          "9": "192.168.1.60 / 70124278"}}

    def fake_post(url, json=None, timeout=None, headers=None):
        if json == {"782": None}:
            return FakeResp({"782": flat782})
        if json == {"740": None}:
            return FakeResp(serials740)
        if isinstance(json, dict) and "143" in json:
            queried.append(list(json["143"]["1"]["101"].keys())[0])
        d = _detail_post(json)
        return d if d is not None else _open_post(json)

    monkeypatch.setattr("experanto_edge.readers.solarlog_getjp.requests.post", fake_post)
    SolarlogGetjpReader("192.168.1.50", spacing=0, collect_detail=True).read()
    assert sorted(queried) == ["0", "1"]   # solo i 2 inverter reali, non 30 slot


def test_discover_returns_782(monkeypatch):
    monkeypatch.setattr(
        "experanto_edge.readers.solarlog_getjp.requests.post",
        lambda url, json=None, timeout=None, headers=None: FakeResp(DEV),
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


# ---- Dettaglio per-inverter (860 channels.min + 143:101 valori correnti) ----


def test_detail_via_csrf_without_login(monkeypatch):
    # Datalogger SENZA password che espone il dettaglio col solo header CSRF: niente
    # login, ma il reader prende comunque 860 (epoch corrente) + 143:101 per inverter.
    def fake_post(url, json=None, timeout=None, headers=None):
        if isinstance(json, dict) and ("143" in json or "860" in json):
            assert headers and headers.get("x-sl-csrf-protection") == "1"
        d = _detail_post(json)
        return d if d is not None else _open_post(json)

    monkeypatch.setattr("experanto_edge.readers.solarlog_getjp.requests.post", fake_post)
    monkeypatch.setattr(
        "experanto_edge.readers.solarlog_getjp.requests.Session",
        lambda: (_ for _ in ()).throw(AssertionError("nessun login senza password")),
    )
    out = SolarlogGetjpReader("192.168.1.50", spacing=0).read()  # niente user_password
    # 860: solo l'epoch corrente (indice piu' alto = 1), non la 0.
    assert set(out["getjp"]["860"].keys()) == {"1"}
    # 143:101: valori correnti raw per inverter, forwardati come [header, [valori]].
    assert out["getjp"]["143"]["0"] == [[1000, 2000, 300], [5000, 41]]
    assert out["getjp"]["143"]["1"] == [[1000, 2000, 300], [6000, 43]]
    assert out["getjp"]["782"] == DEV                     # dati open invariati


def test_detail_extracted_when_password_set(monkeypatch):
    # Datalogger protetto: login "user" bcrypt, poi 860 + 143 via sessione + CSRF.
    class FakeSession:
        def __init__(self):
            self.logged_in = False
            self.login_body = None

        def post(self, url, json=None, data=None, timeout=None, headers=None):
            if url.endswith("/login"):
                self.logged_in = True
                self.login_body = data
                return FakeResp(text="SUCCESS - login ok")
            if url.endswith("/getjp"):
                assert headers and headers.get("x-sl-csrf-protection") == "1"
                d = _detail_post(json)
                if d is not None:
                    return d
            raise AssertionError(f"unexpected session post {url} {json}")

    sess = FakeSession()
    monkeypatch.setattr(
        "experanto_edge.readers.solarlog_getjp.requests.post",
        lambda url, json=None, timeout=None, headers=None: _open_post(json),
    )
    monkeypatch.setattr(
        "experanto_edge.readers.solarlog_getjp.requests.Session", lambda: sess
    )
    out = SolarlogGetjpReader("192.168.1.50", spacing=0, user_password="Calme1234@").read()

    assert sess.logged_in
    assert sess.login_body["u"] == "user"
    assert sess.login_body["p"].startswith("$2b$")        # hash, non la password in chiaro
    assert sess.login_body["p"] != "Calme1234@"
    assert set(out["getjp"]["860"].keys()) == {"1"}
    assert out["getjp"]["143"]["0"] == [[1000, 2000, 300], [5000, 41]]
    assert out["getjp"]["782"] == DEV


def test_860_is_cached_and_resent_without_refetch(monkeypatch):
    # 860 e' statico: si rifetcha sulla cadenza storico ma va incluso in OGNI snapshot
    # (il server e' stateless). Al 2o ciclo non deve re-interrogare 860, ma re-inviarlo.
    epoch_queries = {"n": 0}

    def fake_post(url, json=None, timeout=None, headers=None):
        if isinstance(json, dict) and "860" in json:
            epoch_queries["n"] += 1
        d = _detail_post(json)
        return d if d is not None else _open_post(json)

    monkeypatch.setattr("experanto_edge.readers.solarlog_getjp.requests.post", fake_post)
    r = SolarlogGetjpReader("192.168.1.50", spacing=0, history_interval=9999)
    out1 = r.read()
    n_after_first = epoch_queries["n"]
    out2 = r.read()
    assert epoch_queries["n"] == n_after_first        # nessun re-fetch 860 al 2o ciclo
    assert out1["getjp"]["860"] == out2["getjp"]["860"]  # ma re-inviato dalla cache


def test_detail_absent_when_login_fails(monkeypatch):
    class FailSession:
        def post(self, url, json=None, data=None, timeout=None, headers=None):
            if url.endswith("/login"):
                return FakeResp(text="FAILED - Password was wrong")
            raise AssertionError("non deve interrogare 143 senza login")

    monkeypatch.setattr(
        "experanto_edge.readers.solarlog_getjp.requests.post",
        lambda url, json=None, timeout=None, headers=None: _open_post(json),
    )
    monkeypatch.setattr(
        "experanto_edge.readers.solarlog_getjp.requests.Session", lambda: FailSession()
    )
    # DL protetto (open_post nega 143/860 col 403) + login fallito -> niente dettaglio.
    out = SolarlogGetjpReader("192.168.1.50", spacing=0, user_password="wrong").read()
    assert "143" not in out["getjp"]        # login fallito -> niente dettaglio...
    assert out["getjp"]["782"] == DEV       # ...ma i dati open ci sono comunque


def test_login_not_retried_every_cycle_after_failure(monkeypatch):
    calls = {"login": 0}

    class CountingSession:
        def post(self, url, json=None, data=None, timeout=None, headers=None):
            if url.endswith("/login"):
                calls["login"] += 1
                return FakeResp(text="FAILED")
            raise AssertionError("no 143 senza login")

    monkeypatch.setattr(
        "experanto_edge.readers.solarlog_getjp.requests.post",
        lambda url, json=None, timeout=None, headers=None: _open_post(json),
    )
    monkeypatch.setattr(
        "experanto_edge.readers.solarlog_getjp.requests.Session", lambda: CountingSession()
    )
    r = SolarlogGetjpReader("192.168.1.50", spacing=0, user_password="wrong",
                            history_interval=9999)
    r.read()
    r.read()
    assert calls["login"] == 1               # backoff: non rifa' login a ogni ciclo
