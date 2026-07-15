import pytest

from experanto_edge.readers.base import ReaderError
from experanto_edge.readers.solarlog_getjp import SolarlogGetjpReader

AGG = {"801": {"170": {"101": 12345, "102": 12800, "105": 42000, "116": 780000}}}
DEV = {"782": {"0": {"101": 6000}, "1": {"101": 6345}}}
STATUS = {"608": {"0": "Normal", "1": "OFFLINE"}}
SALTS = {"550": {"104": "$2b$08$qk5birSAh8VntKj4PkvTaO", "112": 1, "113": 1}}
# 143: [[from, to, interval], [[time, [~channels]], ...]] — il reader tiene solo l'ultima riga.
DETAIL_0 = {"143": {"1": {"100": {"0": [[1000, 2000, 300],
                                       [["05:35:00", [1, 2, 3]], ["05:40:00", [4, 5, 6]]]]}}}}
DETAIL_1 = {"143": {"1": {"100": {"1": [[1000, 2000, 300],
                                       [["05:40:00", [7, 8, 9]]]]}}}}
CHANNELS = {"870": [[10, 0, 0, "L_STATUS", ""], [6, 0, 0, "L_TEMPERATURE", "°C"]]}


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
    if isinstance(json, dict) and ("143" in json or "870" in json):
        return FakeResp(status=403, text="ACCESS DENIED")
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
    assert "143" not in out["getjp"]           # niente password -> niente dettaglio
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
    # collect_detail=False (default dell'AGENTE via config): il 143 NON viene mai
    # interrogato (evita ~1MB/ciclo sul datalogger), ma i blocchi open restano.
    def fake_post(url, json=None, timeout=None, headers=None):
        if isinstance(json, dict) and "143" in json:
            raise AssertionError("143 non deve essere interrogato con collect_detail=False")
        r = _open_post(json)
        return r if r is not None else FakeResp(DEV)

    monkeypatch.setattr("experanto_edge.readers.solarlog_getjp.requests.post", fake_post)
    out = SolarlogGetjpReader("192.168.1.50", spacing=0, user_password="x",
                              collect_detail=False).read()
    assert "143" not in out["getjp"]
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
    # SOLO su quei 2 (non su tutti gli slot -> niente scarico dell'intera giornata x30).
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
            dev = list(json["143"]["1"]["100"].keys())[0]
            queried.append(dev)
            return FakeResp(DETAIL_0 if dev == "0" else DETAIL_1)
        if json == {"870": None}:
            return FakeResp(CHANNELS)
        return _open_post(json)

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


# ---- Login "user" + dettaglio per-inverter (143/870) ----

class FakeSession:
    """Sessione loggata: /login -> SUCCESS, /getjp privilegiato -> 143/870."""
    def __init__(self):
        self.logged_in = False
        self.login_body = None

    def post(self, url, json=None, data=None, timeout=None, headers=None):
        if url.endswith("/login"):
            self.logged_in = True
            self.login_body = data
            return FakeResp(text="SUCCESS - login ok")
        if url.endswith("/getjp"):
            assert headers and headers.get("x-sl-csrf-protection") == "1", "manca header CSRF"
            if json == {"143": {"1": {"100": {"0": None}}}}:
                return FakeResp(DETAIL_0)
            if json == {"143": {"1": {"100": {"1": None}}}}:
                return FakeResp(DETAIL_1)
            if json == {"870": None}:
                return FakeResp(CHANNELS)
        raise AssertionError(f"unexpected session post {url} {json}")


def _patch_login(monkeypatch, session):
    monkeypatch.setattr(
        "experanto_edge.readers.solarlog_getjp.requests.post",
        lambda url, json=None, timeout=None, headers=None: _open_post(json),
    )
    monkeypatch.setattr(
        "experanto_edge.readers.solarlog_getjp.requests.Session", lambda: session
    )


def test_detail_extracted_when_password_set(monkeypatch):
    sess = FakeSession()
    _patch_login(monkeypatch, sess)
    r = SolarlogGetjpReader("192.168.1.50", spacing=0, user_password="Calme1234@")
    out = r.read()

    # login avvenuto come "user" con hash bcrypt (non la password in chiaro)
    assert sess.logged_in
    assert sess.login_body["u"] == "user"
    assert sess.login_body["p"].startswith("$2b$")
    assert sess.login_body["p"] != "Calme1234@"

    # dettaglio: solo l'ULTIMA riga per inverter, header preservato
    det = out["getjp"]["143"]
    assert det["0"] == [[1000, 2000, 300], [["05:40:00", [4, 5, 6]]]]
    assert det["1"] == [[1000, 2000, 300], [["05:40:00", [7, 8, 9]]]]
    # dizionario canali 870 inoltrato come sola lista
    assert out["getjp"]["870"] == CHANNELS["870"]
    # i dati open restano invariati (nessuna regressione)
    assert out["getjp"]["782"] == DEV


def test_detail_via_csrf_without_login(monkeypatch):
    # Datalogger SENZA password che espone il dettaglio col solo header CSRF: niente
    # login, ma il reader prende comunque temperatura/tensioni (caso "install pulito").
    def fake_post(url, json=None, timeout=None, headers=None):
        if isinstance(json, dict) and "143" in json:
            assert headers and headers.get("x-sl-csrf-protection") == "1"
            dev = list(json["143"]["1"]["100"].keys())[0]
            return FakeResp(DETAIL_0 if dev == "0" else DETAIL_1)
        if json == {"870": None}:
            assert headers and headers.get("x-sl-csrf-protection") == "1"
            return FakeResp(CHANNELS)
        return _open_post(json)

    monkeypatch.setattr("experanto_edge.readers.solarlog_getjp.requests.post", fake_post)
    monkeypatch.setattr(
        "experanto_edge.readers.solarlog_getjp.requests.Session",
        lambda: (_ for _ in ()).throw(AssertionError("nessun login senza password")),
    )
    out = SolarlogGetjpReader("192.168.1.50", spacing=0).read()  # niente user_password
    det = out["getjp"]["143"]
    assert det["0"] == [[1000, 2000, 300], [["05:40:00", [4, 5, 6]]]]
    assert det["1"] == [[1000, 2000, 300], [["05:40:00", [7, 8, 9]]]]
    assert out["getjp"]["870"] == CHANNELS["870"]


def test_detail_picks_last_row_with_data(monkeypatch):
    # Di notte gli slot recenti sono tutti None: il reader deve forwardare l'ultima riga
    # con dati reali (col timestamp), e saltare del tutto un inverter spento tutto il di'.
    NIGHT = {"143": {"1": {"100": {"0": [[1000, 2000, 300],
             [["06:50:00", [5738, 48]], ["23:50:00", [None, None]],
              ["23:55:00", [None, None]]]]}}}}
    OFF = {"143": {"1": {"100": {"1": [[1000, 2000, 300],
           [["23:55:00", [None, None]]]]}}}}

    def fake_post(url, json=None, timeout=None, headers=None):
        if isinstance(json, dict) and "143" in json:
            dev = list(json["143"]["1"]["100"].keys())[0]
            return FakeResp(NIGHT if dev == "0" else OFF)
        if json == {"870": None}:
            return FakeResp(CHANNELS)
        return _open_post(json)

    monkeypatch.setattr("experanto_edge.readers.solarlog_getjp.requests.post", fake_post)
    out = SolarlogGetjpReader("192.168.1.50", spacing=0).read()
    det = out["getjp"]["143"]
    assert det["0"] == [[1000, 2000, 300], [["06:50:00", [5738, 48]]]]  # ultimo con dati
    assert "1" not in det                                                # spento -> assente


def test_detail_absent_when_login_fails(monkeypatch):
    class FailSession(FakeSession):
        def post(self, url, json=None, data=None, timeout=None, headers=None):
            if url.endswith("/login"):
                return FakeResp(text="FAILED - Password was wrong")
            raise AssertionError("non deve interrogare 143 senza login")

    _patch_login(monkeypatch, FailSession())
    out = SolarlogGetjpReader("192.168.1.50", spacing=0, user_password="wrong").read()
    assert "143" not in out["getjp"]        # login fallito -> niente dettaglio...
    assert out["getjp"]["782"] == DEV       # ...ma i dati open ci sono comunque


def test_login_not_retried_every_cycle_after_failure(monkeypatch):
    calls = {"login": 0}

    class CountingSession(FakeSession):
        def post(self, url, json=None, data=None, timeout=None, headers=None):
            if url.endswith("/login"):
                calls["login"] += 1
                return FakeResp(text="FAILED")
            raise AssertionError("no 143 senza login")

    _patch_login(monkeypatch, CountingSession())
    r = SolarlogGetjpReader("192.168.1.50", spacing=0, user_password="wrong",
                            history_interval=9999)
    r.read()
    r.read()
    assert calls["login"] == 1               # backoff: non rifa' login a ogni ciclo


def test_session_reused_across_cycles(monkeypatch):
    sess = FakeSession()
    logins = {"n": 0}
    orig_post = sess.post

    def counting_post(url, **kw):
        if url.endswith("/login"):
            logins["n"] += 1
        return orig_post(url, **kw)

    sess.post = counting_post
    _patch_login(monkeypatch, sess)
    r = SolarlogGetjpReader("192.168.1.50", spacing=0, user_password="Calme1234@")
    r.read()
    r.read()
    assert logins["n"] == 1                   # login una volta sola, sessione riusata
