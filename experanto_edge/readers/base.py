"""Reader plugin interface.

A reader is a *thin relay*: it fetches raw data from a local datalogger and returns it
verbatim. Parsing into Experanto's universal schema happens server-side, so the fleet
never needs a firmware/agent update when the parsing logic evolves.

Future readers (Modbus TCP/RTU, RS-485, RS-232, CAN, SunSpec) implement this same
interface — see the project plan. They are out of scope for phase E0.
"""
from __future__ import annotations

import abc
from typing import Any, Dict


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
