"""OTA updates — two scopes:

  * update_agent  — the Experanto Edge program itself
  * update_system — the Raspberry Pi OS / packages

Both are server-initiated. The full implementation (signed releases, checksum + signature
verification, health-check, automatic rollback for the agent) lands in phase E5. E0 ships
safe, structured stubs that refuse to do anything until E5 wires the signing/rollback path,
plus a working `os_version()` helper used in the status payload.
"""
from __future__ import annotations

import subprocess
from typing import Optional, Tuple


def update_agent(cfg, version: Optional[str]) -> Tuple[bool, str]:
    # E5 flow: download signed release for `version` -> verify checksum + signature ->
    #          swap venv -> restart service -> health-check -> rollback on failure.
    if not version:
        return False, "versione non specificata"
    return False, "OTA agente non ancora abilitato (fase E5)"


def update_system(cfg, mode: str = "security") -> Tuple[bool, str]:
    # E5 flow: apt-get update && (security-only | full) upgrade, optional reboot,
    #          then report os_version back in the status payload.
    if mode not in ("security", "full"):
        return False, f"mode non valido: {mode}"
    return False, "OTA sistema non ancora abilitato (fase E5)"


def os_version() -> str:
    """Best-effort OS description for the status payload."""
    try:
        out = subprocess.run(
            ["lsb_release", "-ds"], capture_output=True, text=True, timeout=5
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return "unknown"
