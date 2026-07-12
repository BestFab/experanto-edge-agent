"""Remote access — WireGuard tunnel to a SELF-HOSTED hub (no third-party service).

The Pi has no inbound ports (often CGNAT). It joins YOUR WireGuard hub — a single UDP port
on your VPS, which coexists with nginx/sshd on the same host (even UDP/443) — as a peer with
a fixed overlay IP; you then SSH straight to that IP. On `open_ssh` the agent brings the WG
interface up for a bounded window, then tears it down. Nothing external: only wireguard-tools
and your own VPS.

`wg-quick` creates a network interface → needs root: routed via `sudo -n` (install.sh adds a
scoped NOPASSWD rule for exactly `wg-quick up/down <iface>`). The keys/endpoint/peer live in
/etc/wireguard/<iface>.conf; here we only need the interface name + this Pi's overlay IP.

`_run` is the single subprocess seam so tests don't need real WireGuard.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Tuple

log = logging.getLogger("experanto-edge.remote")

_CMD_TIMEOUT = 25
WG_DIR = "/etc/wireguard"


def _iface(cfg) -> str:
    return getattr(cfg, "wg_interface", "") or "wg-experanto"


def _conf_path(cfg) -> str:
    return os.path.join(WG_DIR, f"{_iface(cfg)}.conf")


def _run(args, timeout=_CMD_TIMEOUT) -> subprocess.CompletedProcess:
    """Invoke a command. Wrapped so tests can monkeypatch it."""
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _wgquick(action: str, iface: str) -> Tuple[bool, str]:
    try:
        r = _run(["sudo", "-n", "wg-quick", action, iface])
    except Exception as e:
        return False, str(e)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "").strip()[:200]
    return True, ""


def _validate(cfg) -> str:
    if not shutil.which("wg-quick"):
        return "wireguard-tools non installato (wg-quick assente)"
    conf = _conf_path(cfg)
    if not os.path.exists(conf):
        return f"config WireGuard assente: {conf}"
    if not getattr(cfg, "wg_address", ""):
        return "wg_address non configurato"
    return ""


def up(cfg, ttl: int = 0) -> Tuple[bool, object]:
    """Bring the WG interface up. Returns (True, {"reach","address","interface"}) or (False, reason)."""
    err = _validate(cfg)
    if err:
        return False, err
    iface = _iface(cfg)
    _wgquick("down", iface)                      # best-effort: evita l'errore "already exists"
    ok, detail = _wgquick("up", iface)
    if not ok:
        return False, f"wg-quick up fallito: {detail}"
    addr = getattr(cfg, "wg_address", "").split("/")[0].strip()
    user = getattr(cfg, "wg_ssh_user", "") or "<utente_pi>"
    return True, {"reach": f"ssh {user}@{addr}", "address": addr, "interface": iface}


def down(cfg) -> Tuple[bool, str]:
    """Tear the WG interface down (idempotente)."""
    _wgquick("down", _iface(cfg))
    return True, ""
