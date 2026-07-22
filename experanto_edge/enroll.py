"""First-run enrollment and local datalogger discovery.

Enrollment writes the device identity (code + secret, optionally station_id / broker)
into the config. Datalogger discovery is a best-effort LAN scan for a host answering the
Solar-Log getjp API — the user can always set `datalogger_ip` manually.
"""
from __future__ import annotations

import ipaddress
import socket
import time
from concurrent import futures
from typing import List, Optional

import requests


def bootstrap(
    cfg,
    device_code: Optional[str],
    secret: Optional[str],
    station_id: Optional[str] = None,
    broker_host: Optional[str] = None,
) -> None:
    """Write identity into config on first run (idempotent)."""
    if device_code:
        cfg.device_code = device_code
    if secret:
        cfg.secret = secret
    if station_id:
        cfg.station_id = station_id
    if broker_host:
        cfg.broker_host = broker_host
    cfg.save()


def probe_getjp(ip: str, port: int = 80, timeout: float = 2.0) -> bool:
    try:
        r = requests.post(
            f"http://{ip}:{port}/getjp", json={"801": {"170": None}}, timeout=timeout
        )
        return r.status_code == 200 and isinstance(r.json(), dict)
    except Exception:
        return False


def local_subnet() -> Optional[str]:
    """The /24 the Pi is on (derived from the default-route source address)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return str(ipaddress.ip_network(f"{ip}/24", strict=False))
    except Exception:
        return None


def discover_datalogger(subnet: Optional[str] = None, port: int = 80,
                        timeout: float = 0.4, budget: float = 20.0,
                        workers: int = 32) -> Optional[str]:
    """Best-effort scan for a host answering the Solar-Log getjp API.

    Concorrente a ONDATE con budget di tempo: fino a 0.3.x la scansione era
    sequenziale (254 host x 0.4s ~ 100s BLOCCANTI all'avvio; con Restart=always
    + RestartSec=10 un datalogger spento innescava cicli scan/riavvio). Ora ogni
    ondata sonda `workers` host in parallelo (una /24 vuota ~ 8 ondate ~ 3s) e
    allo scadere di `budget` si ritorna None senza aspettare il giro completo.
    Deterministico come la scansione storica: fra piu' host che rispondono vince
    quello piu' basso nell'ordine di subnet (ondate in ordine; dentro l'ondata
    si valuta in ordine) — conta con due datalogger sulla stessa LAN (.57/.59).
    Prefer setting datalogger_ip manually when known."""
    net = subnet or local_subnet()
    if not net:
        return None
    hosts = [str(h) for h in ipaddress.ip_network(net).hosts()]
    deadline = time.monotonic() + budget
    for i in range(0, len(hosts), workers):
        if time.monotonic() >= deadline:
            return None  # budget esaurito: meglio partire senza che bloccare il loop
        wave = hosts[i:i + workers]
        with futures.ThreadPoolExecutor(max_workers=len(wave)) as ex:
            hits = list(ex.map(lambda h: probe_getjp(h, port, timeout), wave))
        for host, hit in zip(wave, hits):
            if hit:
                return host
    return None


def local_ips() -> List[str]:
    ips: List[str] = []
    # Primary source IP first. gethostname() alone is unreliable on Debian/Raspberry Pi
    # OS, where the hostname maps to 127.0.1.1 (filtered below) and the real LAN address
    # is never returned — leaving the status payload with no reachable IP for SSH.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no packet sent; just picks the default-route source
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            if addr not in ips and not addr.startswith("127."):
                ips.append(addr)
    except Exception:
        pass
    return ips
