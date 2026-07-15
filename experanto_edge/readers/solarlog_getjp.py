"""Reader for Solar-Log Base 2000 (and family) via the local `getjp` JSON API.

Thin relay: it forwards the raw getjp responses; the server parses the numeric indices
into fields. Requires the datalogger's local API access to be set to "Open".

getjp queries (Solar-Log Base handbook + reverse-engineering):
  {"801": {"170": null}}  -> plant aggregate. Notable indices in 801/170:
        101 = Pac (W), 102 = Pdc (W), 105/106 = yield today/yesterday (Wh),
        109 = yield total (Wh), 116 = installed generator power (Wp)
  {"782": null}           -> per-device AC power list, indexed 0..N (flat: {idx: "W"})
  {"608": null}           -> per-device status, indexed 0..N ("Normal"/"OFFLINE"/
                             "RUNNING"). "RUNNING" marks a meter (not an inverter).
Note: this firmware does NOT expose per-inverter name/nominal (141), temperature,
DC voltage or MPPT via getjp — the server parser works with power + status only.
"""
from __future__ import annotations

import time
from typing import Any, Dict

import requests

from .base import Reader, ReaderError

QUERY_AGGREGATE = {"801": {"170": None}}
QUERY_DEVICES = {"782": None}
QUERY_STATUS = {"608": None}


class SolarlogGetjpReader(Reader):
    reader_type = "solarlog_getjp"

    def __init__(self, ip: str, port: int = 80, timeout: float = 10.0, spacing: float = 1.5):
        # NON sollevare qui: un datalogger assente/non ancora configurato non deve far
        # crashare l'agente al boot. L'errore emerge in read() -> lo cattura run_cycle,
        # che riporta lo stato "errore" e ritenta al ciclo dopo (niente crash-loop).
        self.ip = ip
        self.base_url = f"http://{ip}:{port}"
        self.timeout = timeout
        # Il Solar-Log risponde 503 se interrogato troppo in fretta: spaziamo le query.
        self.spacing = spacing

    def _getjp(self, query: Dict[str, Any]) -> Any:
        if not self.ip:
            raise ReaderError("datalogger_ip non configurato (datalogger assente o non ancora impostato)")
        try:
            r = requests.post(f"{self.base_url}/getjp", json=query, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            raise ReaderError(f"getjp {query} fallita su {self.base_url}: {e}") from e

    def _getjp_optional(self, query: Dict[str, Any]) -> Any:
        """Come _getjp ma best-effort: None se fallisce (503/rete).

        Usato per il blocco status 608: un suo errore NON deve far perdere la
        lettura di potenza (che è il dato primario) né innescare un errore-ciclo.
        """
        try:
            return self._getjp(query)
        except ReaderError:
            return None

    def read(self) -> Dict[str, Any]:
        # Aggregate + per-device power + per-device status: tutti dati live.
        # Le query sono spaziate (il datalogger 503-a se troppo veloce); 608 è
        # best-effort (server parser -> fallback senza status se assente).
        agg = self._getjp(QUERY_AGGREGATE)
        time.sleep(self.spacing)
        devices = self._getjp(QUERY_DEVICES)
        time.sleep(self.spacing)
        status = self._getjp_optional(QUERY_STATUS)
        return {
            "read_at": int(time.time()),
            "getjp": {"801_170": agg, "782": devices, "608": status},
        }

    def discover(self) -> Dict[str, Any]:
        devices = self._getjp(QUERY_DEVICES)
        time.sleep(self.spacing)
        status = self._getjp_optional(QUERY_STATUS)
        return {"read_at": int(time.time()), "getjp": {"782": devices, "608": status}}
