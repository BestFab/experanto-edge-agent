"""Autodiscovery LAN: concorrente a ondate, con budget di tempo, deterministica.

Fino a 0.3.x discover_datalogger scansionava 254 host IN SEQUENZA a timeout
0.4s: fino a ~100s bloccanti prima del loop (e con Restart=always+RestartSec=10
un datalogger spento innescava cicli scan/riavvio). Tutto offline: probe_getjp
e' monkeypatchata, nessuna rete richiesta.
"""
import time

from experanto_edge import enroll


def test_discovery_finds_host_concurrently(monkeypatch):
    # host in fondo alla /24: in sequenza sarebbero ~200 probe x 50ms = ~10s,
    # a ondate da 32 sono ~7 ondate x 50ms -> deve chiudere in pochi decimi.
    def fake_probe(ip, port=80, timeout=2.0):
        time.sleep(0.05)
        return ip == "10.9.0.200"

    monkeypatch.setattr(enroll, "probe_getjp", fake_probe)
    t0 = time.monotonic()
    assert enroll.discover_datalogger(subnet="10.9.0.0/24") == "10.9.0.200"
    assert time.monotonic() - t0 < 3.0


def test_discovery_respects_time_budget(monkeypatch):
    # LAN morta con probe lente: il budget deve tagliare la scansione molto
    # prima del giro completo (254 x 0.2s sequenziali sarebbero ~51s).
    def fake_probe(ip, port=80, timeout=2.0):
        time.sleep(0.2)
        return False

    monkeypatch.setattr(enroll, "probe_getjp", fake_probe)
    t0 = time.monotonic()
    assert enroll.discover_datalogger(subnet="10.9.0.0/24", budget=0.5, workers=8) is None
    assert time.monotonic() - t0 < 3.0


def test_discovery_prefers_lowest_host(monkeypatch):
    # Determinismo con DUE datalogger sulla stessa LAN (caso reale .57/.59):
    # vince l'host piu' basso, come nella scansione sequenziale storica.
    hits = {"10.9.0.57", "10.9.0.59"}
    monkeypatch.setattr(enroll, "probe_getjp",
                        lambda ip, port=80, timeout=2.0: ip in hits)
    assert enroll.discover_datalogger(subnet="10.9.0.0/24") == "10.9.0.57"


def test_discovery_no_subnet(monkeypatch):
    monkeypatch.setattr(enroll, "local_subnet", lambda: None)
    assert enroll.discover_datalogger() is None
