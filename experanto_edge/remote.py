"""Remote access — on-demand Tailscale tunnel for SSH into a Pi behind NAT.

A Pi at a customer site has no inbound ports (often CGNAT), so we cannot reach it
directly. Instead the agent dials OUT: on the `open_ssh` command it brings a Tailscale
tunnel UP for a bounded window, then tears it down. Tailscale runs in *operator* mode
(install.sh does `tailscale set --operator=<svc user>`), so these commands need no sudo.
Works against Tailscale SaaS or a self-hosted Headscale (`tailscale_login_server`).

Everything routes through `_run` so tests can stub the subprocess without a real tailscaled.
"""
from __future__ import annotations

import logging
import subprocess
from typing import List, Tuple

log = logging.getLogger("experanto-edge.remote")

_UP_TIMEOUT = 60          # s: how long `tailscale up` waits for the backend to settle
_CMD_TIMEOUT = 15         # s: for quick queries (ip / down)


def _run(args: List[str], timeout: int) -> subprocess.CompletedProcess:
    """Invoke the tailscale CLI. Wrapped so tests can monkeypatch it."""
    return subprocess.run(
        ["tailscale", *args], capture_output=True, text=True, timeout=timeout
    )


def up(cfg, authkey: str, ttl: int) -> Tuple[bool, object]:
    """Bring the tunnel up and enable Tailscale SSH. Returns (True, {"ip","host"})
    on success or (False, "<reason>"). `ttl` is informational here — the agent
    enforces the window; we only pass it to bound the backend wait."""
    if not authkey:
        return False, "tailscale_authkey non configurata"
    hostname = (
        getattr(cfg, "tailscale_hostname", "")
        or getattr(cfg, "device_code", "")
        or "experanto-edge"
    )
    args = [
        "up",
        "--ssh",                       # enable Tailscale SSH (ACL-gated, no host keys)
        "--authkey", authkey,
        "--hostname", hostname,
        "--accept-dns=false",          # don't touch the Pi's resolv.conf
        f"--timeout={_UP_TIMEOUT}s",
    ]
    login = getattr(cfg, "tailscale_login_server", "") or ""
    if login:
        args.append(f"--login-server={login}")
    try:
        r = _run(args, _UP_TIMEOUT + 10)
    except Exception as e:  # subprocess/timeout/binary-missing
        return False, f"tailscale up fallito: {e}"
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()[:200]
        return False, f"tailscale up rc={r.returncode}: {detail}"
    return True, {"ip": _ip4(), "host": hostname}


def down(cfg=None) -> Tuple[bool, str]:
    """Disconnect the tunnel (idempotent). Ephemeral nodes auto-expire once down."""
    try:
        r = _run(["down"], _CMD_TIMEOUT)
    except Exception as e:
        return False, f"tailscale down fallito: {e}"
    return r.returncode == 0, (r.stderr or r.stdout or "").strip()


def _ip4() -> str:
    """Best-effort overlay IPv4 (100.x.y.z); '' if unavailable."""
    try:
        r = _run(["ip", "-4"], _CMD_TIMEOUT)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().splitlines()[0].strip()
    except Exception:
        pass
    return ""
