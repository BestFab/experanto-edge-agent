"""Reader plugin interface.

A reader is a *thin relay*: it fetches raw data from a local datalogger and returns it
verbatim. Parsing into Experanto's universal schema happens server-side, so the fleet
never needs a firmware/agent update when the parsing logic evolves.

Future readers (Modbus TCP/RTU, RS-485, RS-232, CAN, SunSpec) implement this same
interface — see the project plan. They are out of scope for phase E0.
"""
from __future__ import annotations

import abc
from typing import Any, Dict, Optional


class ReaderError(Exception):
    """Raised when a reader cannot fetch data from the local device."""


class Reader(abc.ABC):
    reader_type: str = "base"

    @abc.abstractmethod
    def read(self) -> Dict[str, Any]:
        """Return raw datalogger data (verbatim). Raise ReaderError on failure."""

    @abc.abstractmethod
    def discover(self) -> Dict[str, Any]:
        """Return raw device/inverter inventory (verbatim) for server-side discovery."""

    def fetch_history_curves(self, daysback: int) -> Optional[Dict[str, Any]]:
        """On-demand history (the `fetch_history` command): raw per-inverter curve
        of `daysback` days ago, as ``{"ch860": <block>, "curves": {idx: node}}``.

        Nel contratto da 0.4.0: e' il metodo su cui poggia lo storico on-demand,
        prima esisteva solo per duck-typing su SolarlogGetjpReader. Default:
        ``None`` = storico NON supportato — l'agente acka un onesto "senza
        storico on-demand" e non pubblica chunk. I reader che lo supportano fanno
        override ritornando il dict (mai None)."""
        return None
