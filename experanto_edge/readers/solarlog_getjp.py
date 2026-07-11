"""Reader for Solar-Log Base 2000 (and family) via the local `getjp` JSON API.

Thin relay: it forwards the raw getjp responses; the server parses the numeric indices
into fields. Requires the datalogger's local API access to be set to "Open".

getjp queries (Solar-Log Base handbook):
  {"801": {"170": null}}  -> plant aggregate. Notable indices in 801/170:
        101 = Pac (W), 102 = Pdc (W), 105/106 = yield today/yesterday (Wh),
        109 = yield total (Wh), 116 = installed generator power (Wp)
  {"782": null}           -> per-device list (inverters / meters), indexed 0..N
"""
from __future__ import annotations

import time
from typing import Any, Dict

import requests

from .base import Reader, ReaderError

QUERY_AGGREGATE = {"801": {"170": None}}
QUERY_DEVICES = {"782": None}


class SolarlogGetjpReader(Reader):
    reader_type = "solarlog_getjp"

    def __init__(self, ip: str, port: int = 80, timeout: float = 10.0):
        if not ip:
            raise ReaderError("datalogger_ip non configurato")
        self.base_url = f"http://{ip}:{port}"
        self.timeout = timeout

    def _getjp(self, query: Dict[str, Any]) -> Any:
        try:
            r = requests.post(f"{self.base_url}/getjp", json=query, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            raise ReaderError(f"getjp {query} fallita su {self.base_url}: {e}") from e

    def read(self) -> Dict[str, Any]:
        # Both the aggregate and the per-device block are live data — forward both.
        return {
            "read_at": int(time.time()),
            "getjp": {
                "801_170": self._getjp(QUERY_AGGREGATE),
                "782": self._getjp(QUERY_DEVICES),
            },
        }

    def discover(self) -> Dict[str, Any]:
        return {"read_at": int(time.time()), "getjp": {"782": self._getjp(QUERY_DEVICES)}}
