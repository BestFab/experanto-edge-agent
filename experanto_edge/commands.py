"""Command dispatch.

Commands arrive as retained MQTT messages on `dn/cmd`. Each is handled once (dedup by
`command_id`, tracked in config) and acked on `up/ack`. A bad command never crashes the
loop — handlers return (ok, detail) and exceptions are captured.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Tuple

Handler = Callable[[Dict[str, Any]], Tuple[bool, str]]


class CommandDispatcher:
    def __init__(self, agent):
        self.agent = agent
        self.handlers: Dict[str, Handler] = {
            "read_now": self._read_now,
            "set_interval": self._set_interval,
            "rediscover": self._rediscover,
            "get_diag": self._get_diag,
            "restart": self._restart,
            "reboot": self._reboot,
            "update_agent": self._update_agent,
            "update_system": self._update_system,
            "open_ssh": self._open_ssh,
            "close_ssh": self._close_ssh,
        }

    def handle(self, cmd: Dict[str, Any]) -> Tuple[bool, str]:
        name = cmd.get("cmd")
        handler = self.handlers.get(name)
        if handler is None:
            return False, f"comando sconosciuto: {name}"
        try:
            return handler(cmd.get("args") or {})
        except Exception as e:  # a bad command must never kill the loop
            return False, f"errore {name}: {e}"

    # --- handlers ---
    def _read_now(self, args):
        self.agent.request_immediate_read()
        return True, "lettura immediata programmata"

    def _set_interval(self, args):
        val = int(args.get("interval", 0))
        if not (30 <= val <= 86400):
            return False, "interval fuori range [30, 86400]"
        self.agent.cfg.interval = val
        self.agent.cfg.save()
        return True, f"interval={val}"

    def _rediscover(self, args):
        self.agent.request_discovery()
        return True, "discovery programmata al prossimo ciclo"

    def _get_diag(self, args):
        return True, json.dumps(self.agent.diagnostics())

    def _restart(self, args):
        return self.agent.restart_service()

    def _reboot(self, args):
        return self.agent.reboot_host()

    def _update_agent(self, args):
        from . import update
        return update.update_agent(self.agent.cfg, args.get("version"))

    def _update_system(self, args):
        from . import update
        return update.update_system(self.agent.cfg, args.get("mode", "security"))

    def _open_ssh(self, args):
        return self.agent.open_ssh(args)

    def _close_ssh(self, args):
        return self.agent.close_ssh(args)
