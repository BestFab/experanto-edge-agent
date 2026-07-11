"""First-run enrollment and local datalogger discovery.

Enrollment writes the device identity (code + secret, optionally station_id / broker)
into the config. Datalogger discovery is a best-effort LAN scan for a host answering the
Solar-Log getjp API — the user can always set `datalogger_ip` manually.
"""
from __future__ import annotations

import ipaddress
import socket
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


def discover_datalogger(subnet: Optional[str] = None, port: int = 80) -> Optional[str]:
    """Best-effort scan for a host answering the Solar-Log getjp API. Sequential and
    slow (up to ~254 probes); prefer setting datalogger_ip manually when known."""
    net = subnet or local_subnet()
    if not net:
        return None
    for host in ipaddress.ip_network(net).hosts():
        if probe_getjp(str(host), port, timeout=0.4):
            return str(host)
    return None


def local_ips() -> List[str]:
    ips: List[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            if addr not in ips and not addr.startswith("127."):
                ips.append(addr)
    except Exception:
        pass
    return ips
