"""Remote access — reverse SSH tunnel to a SELF-HOSTED bastion (no third-party service).

A Pi at a customer site has no inbound ports (often CGNAT). It keeps an OUTBOUND SSH
connection to YOUR bastion and exposes its own sshd there with a remote-forward
(`ssh -R <reverse_port>:localhost:22`); you reach the Pi through that port. On `open_ssh`
the agent brings the tunnel up for a bounded window, then tears it down. Nothing external
is involved: only openssh (+autossh if installed) and your own VPS.

Spawn/terminate go through `_spawn`/`_terminate` so tests don't need real processes.
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
from typing import Optional, Tuple

log = logging.getLogger("experanto-edge.remote")

_SETTLE = 3                                   # s: se il tunnel esce entro qui, è fallito
_STATE_FALLBACK = "/var/lib/experanto-edge"


def _state_dir(cfg) -> str:
    return os.path.dirname(getattr(cfg, "buffer_path", "") or "") or _STATE_FALLBACK


def _pidfile(cfg) -> str:
    return os.path.join(_state_dir(cfg), "ssh_tunnel.pid")


def _tunnel_cmd(cfg):
    local = int(getattr(cfg, "ssh_local_port", 22) or 22)
    fwd = f"{int(cfg.ssh_reverse_port)}:localhost:{local}"
    known = os.path.join(_state_dir(cfg), "known_hosts_bastion")
    opts = [
        "-N", "-T",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={known}",
        "-o", "IdentitiesOnly=yes",
        "-i", cfg.ssh_identity,
        "-R", fwd,
        "-p", str(int(getattr(cfg, "ssh_bastion_port", 22) or 22)),
        f"{cfg.ssh_bastion_user}@{cfg.ssh_bastion_host}",
    ]
    autossh = shutil.which("autossh")             # se presente, riconnette da solo
    return [autossh, "-M", "0", *opts] if autossh else ["ssh", *opts]


def _validate(cfg) -> str:
    if not getattr(cfg, "ssh_bastion_host", ""):
        return "ssh_bastion_host non configurato"
    if not int(getattr(cfg, "ssh_reverse_port", 0) or 0):
        return "ssh_reverse_port non configurato"
    ident = getattr(cfg, "ssh_identity", "")
    if not ident or not os.path.exists(ident):
        return f"chiave SSH assente: {ident or '(non configurata)'}"
    return ""


# --- process seams (monkeypatched nei test) ---
def _spawn(cmd):
    return subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, start_new_session=True, close_fds=True,
    )


def _terminate(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)     # tutto il gruppo (autossh + ssh)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass


def up(cfg, ttl: int = 0) -> Tuple[bool, object]:
    """Bring the reverse tunnel up. Returns (True, {"reach","bastion","port"}) or (False, reason)."""
    err = _validate(cfg)
    if err:
        return False, err
    down(cfg)                                          # niente tunnel doppi
    try:
        proc = _spawn(_tunnel_cmd(cfg))
    except Exception as e:
        return False, f"avvio tunnel fallito: {e}"
    try:
        rc = proc.wait(timeout=_SETTLE)                # esce entro _SETTLE -> fallito
        detail = ""
        try:
            detail = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()[:200]
        except Exception:
            pass
        return False, f"tunnel uscito subito (rc={rc}): {detail}"
    except subprocess.TimeoutExpired:
        pass                                           # ancora vivo -> ok
    _write_pid(cfg, proc.pid)
    port = int(cfg.ssh_reverse_port)
    reach = f"ssh -J <admin>@{cfg.ssh_bastion_host} -p {port} <utente_pi>@127.0.0.1"
    return True, {"reach": reach, "bastion": cfg.ssh_bastion_host, "port": port}


def down(cfg) -> Tuple[bool, str]:
    """Tear the tunnel down (idempotente)."""
    pid = _read_pid(cfg)
    if pid:
        _terminate(pid)
    _clear_pid(cfg)
    return True, ""


def _write_pid(cfg, pid: int) -> None:
    try:
        os.makedirs(_state_dir(cfg), exist_ok=True)
        with open(_pidfile(cfg), "w") as f:
            f.write(str(pid))
    except Exception as e:
        log.debug("pidfile non scritto: %s", e)


def _read_pid(cfg) -> Optional[int]:
    try:
        with open(_pidfile(cfg)) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _clear_pid(cfg) -> None:
    try:
        os.unlink(_pidfile(cfg))
    except OSError:
        pass
