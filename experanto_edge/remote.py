"""Remote access — WireGuard tunnel to a SELF-HOSTED hub (no third-party service).

The Pi has no inbound ports (often CGNAT). It joins YOUR WireGuard hub — a single UDP port
on your VPS, which coexists with nginx/sshd on the same host (even UDP/443) — as a peer with
a fixed overlay IP; you then SSH straight to that IP.

DUE MODI (indipendenti dall'agente-dati):
- **overlay PERSISTENTE, system-managed** (DEFAULT, `wg_managed_externally=True`): l'interfaccia
  la tiene su `wg-quick@<iface>` abilitato al boot — e' la LIFELINE del Pi. L'agente NON la
  tocca: `up`/`down` sono no-op sull'interfaccia (up conferma solo la raggiungibilita').
  Cosi' un deploy/refactor dell'agente non puo' MAI abbattere la connettivita'.
- **on-demand, agent-managed** (`wg_managed_externally=False`): l'agente porta su l'interfaccia
  a finestra su `open_ssh` e la abbatte allo scadere. Solo dove NON c'e' overlay persistente.

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


def _external(cfg) -> bool:
    """True se l'interfaccia WG e' gestita dal SISTEMA (overlay persistente), non dall'agente.

    Default True: l'agente NON tocca l'interfaccia (nessun wg-quick up/down) -> non puo'
    abbattere la lifeline. False solo dove l'agente possiede un'interfaccia on-demand.
    """
    return bool(getattr(cfg, "wg_managed_externally", True))


def up(cfg, ttl: int = 0) -> Tuple[bool, object]:
    """Porta su l'interfaccia WG, o conferma solo la raggiungibilita' se e' system-managed.

    Ritorna (True, {"reach","address","interface","managed"}) oppure (False, motivo).
    """
    iface = _iface(cfg)
    addr = getattr(cfg, "wg_address", "").split("/")[0].strip()
    user = getattr(cfg, "wg_ssh_user", "") or "<utente_pi>"
    if _external(cfg):
        # Overlay persistente (es. wg-quick@iface abilitato al boot): gia' su, NON lo
        # tocchiamo. open_ssh conferma solo dove/come raggiungere il Pi.
        if not addr:
            return False, "wg_address non configurato"
        return True, {"reach": f"ssh {user}@{addr}", "address": addr,
                      "interface": iface, "managed": "external"}
    err = _validate(cfg)
    if err:
        return False, err
    _wgquick("down", iface)                      # best-effort: evita l'errore "already exists"
    ok, detail = _wgquick("up", iface)
    if not ok:
        return False, f"wg-quick up fallito: {detail}"
    return True, {"reach": f"ssh {user}@{addr}", "address": addr,
                  "interface": iface, "managed": "agent"}


def down(cfg) -> Tuple[bool, str]:
    """Abbatte l'interfaccia WG (idempotente) — SOLO se l'agente la possiede.

    Con overlay persistente (default `wg_managed_externally=True`) e' un NO-OP: la lifeline
    del Pi non va MAI abbattuta dall'agente (la gestisce wg-quick@<iface> di sistema).
    """
    if _external(cfg):
        return True, "overlay persistente (system-managed): interfaccia non toccata"
    _wgquick("down", _iface(cfg))
    return True, ""
