"""Experanto Edge agent — entry point and cycle loop.

Each cycle (every `interval` seconds): read the local datalogger, connect to the broker,
flush any buffered readings, publish telemetry + status, pick up one retained command,
ack it, and disconnect. No permanent connection is held — offline is detected server-side
from stale telemetry, exactly like any other plant.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time

from . import __version__, enroll, update
from .buffer import Buffer
from .commands import CommandDispatcher
from .config import Config
from .readers.base import Reader, ReaderError
from .readers.solarlog_getjp import SolarlogGetjpReader
from .transport import MqttTransport, Transport, TransportError

log = logging.getLogger("experanto-edge")

# reader_type -> factory. Future buses (modbus/rs485/can/...) register here (phase: handoff).
READERS = {
    "solarlog_getjp": lambda cfg: SolarlogGetjpReader(cfg.datalogger_ip, cfg.datalogger_port),
}


def build_reader(cfg: Config) -> Reader:
    factory = READERS.get(cfg.reader_type)
    if factory is None:
        raise ReaderError(f"reader_type sconosciuto: {cfg.reader_type}")
    return factory(cfg)


def _discover_datalogger(cfg: Config) -> None:
    ip = enroll.discover_datalogger(port=cfg.datalogger_port)
    if ip:
        cfg.datalogger_ip = ip
        cfg.save()
        log.info("datalogger trovato a %s", ip)
    else:
        log.warning("datalogger non trovato in autodiscovery; imposta datalogger_ip a mano")


class Agent:
    def __init__(self, cfg: Config, reader: Reader, transport: Transport, buffer: Buffer):
        self.cfg = cfg
        self.reader = reader
        self.transport = transport
        self.buffer = buffer
        self.dispatcher = CommandDispatcher(self)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._want_discovery = False
        self._started = int(time.time())

    # --- command effects ---
    def request_immediate_read(self) -> None:
        self._wake.set()

    def request_discovery(self) -> None:
        self._want_discovery = True
        self._wake.set()

    def restart_service(self):
        self._stop.set()
        self._wake.set()
        return True, "riavvio (systemd Restart=always riavvia il processo)"

    def reboot_host(self):
        return update.reboot(self.cfg)

    def diagnostics(self) -> dict:
        return {
            "agent_version": __version__,
            "os_version": update.os_version(),
            "reader_type": self.cfg.reader_type,
            "datalogger_ip": self.cfg.datalogger_ip,
            "interval": self.cfg.interval,
            "buffered": self.buffer.count(),
            "uptime_s": int(time.time()) - self._started,
        }

    # --- payload builders ---
    def _telemetry(self, data: dict) -> dict:
        return {
            "schema": "experanto.edge.telemetry/1",
            "device_code": self.cfg.device_code,
            "station_id": self.cfg.station_id,
            "reader_type": self.cfg.reader_type,
            "agent_version": __version__,
            "read_at": data.get("read_at", int(time.time())),
            "data": data,
        }

    def _status(self, error: str = "") -> dict:
        d = self.diagnostics()
        d.update(
            {
                "schema": "experanto.edge.status/1",
                "device_code": self.cfg.device_code,
                "station_id": self.cfg.station_id,
                "local_ips": enroll.local_ips(),
                "at": int(time.time()),
                "error": error,
            }
        )
        return d

    def _ack(self, cmd: dict, ok: bool, detail: str) -> dict:
        return {
            "schema": "experanto.edge.ack/1",
            "device_code": self.cfg.device_code,
            "command_id": cmd.get("command_id"),
            "cmd": cmd.get("cmd"),
            "ok": ok,
            "detail": detail,
            "at": int(time.time()),
        }

    # --- cycle ---
    def run_cycle(self) -> None:
        error = ""
        telemetry = None
        try:
            data = self.reader.read()
            if self._want_discovery:
                data["discover"] = self.reader.discover()
                self._want_discovery = False
            telemetry = self._telemetry(data)
        except ReaderError as e:
            error = str(e)
            log.warning("lettura datalogger fallita: %s", e)

        try:
            self.transport.connect()
        except TransportError as e:
            log.warning("broker non raggiungibile (%s) — bufferizzo", e)
            if telemetry:
                self.buffer.append(self.cfg.topic("up/telemetry"), telemetry)
            return

        try:
            self._flush_buffer()
            if telemetry and not self.transport.publish(self.cfg.topic("up/telemetry"), telemetry):
                self.buffer.append(self.cfg.topic("up/telemetry"), telemetry)
            self.transport.publish(self.cfg.topic("up/status"), self._status(error))
            self._handle_command()
        finally:
            self.transport.disconnect()

    def _flush_buffer(self) -> None:
        done = []
        for rid, topic, payload in self.buffer.pending():
            if self.transport.publish(topic, payload):
                done.append(rid)
            else:
                break  # broker degraded — stop, keep the rest for next time
        self.buffer.delete(done)

    def _handle_command(self) -> None:
        cmd = self.transport.get_retained_command(self.cfg.topic("dn/cmd"), self.cfg.command_wait)
        if not cmd:
            return
        cid = cmd.get("command_id")
        if cid and cid == self.cfg.last_command_id:
            return  # already handled (retained messages persist until cleared)
        ok, detail = self.dispatcher.handle(cmd)
        self.transport.publish(self.cfg.topic("up/ack"), self._ack(cmd, ok, detail))
        if cid:
            self.cfg.last_command_id = cid
            self.cfg.save()

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: (self._stop.set(), self._wake.set()))
        signal.signal(signal.SIGINT, lambda *_: (self._stop.set(), self._wake.set()))
        log.info(
            "avvio agente %s device=%s interval=%ss",
            __version__, self.cfg.device_code, self.cfg.interval,
        )
        update.write_health_marker(self.cfg)  # signal "process is up" ASAP (OTA rollback watches it)
        while not self._stop.is_set():
            start = time.time()
            try:
                self.run_cycle()
            except Exception:
                log.exception("errore nel ciclo")
            update.write_health_marker(self.cfg)
            self._wake.clear()
            self._wake.wait(max(0.0, self.cfg.interval - (time.time() - start)))
        log.info("arresto agente")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="experanto-edge")
    ap.add_argument("--config", help="percorso config.yaml")
    ap.add_argument("--once", action="store_true", help="esegue un solo ciclo ed esce")
    ap.add_argument("--enroll", help="scrive CODE:SECRET[:STATION_ID] in config")
    ap.add_argument("--discover-datalogger", action="store_true", help="scansiona la LAN")
    ap.add_argument("--selfcheck", action="store_true",
                    help="probe di salute (import+config+transport), usato dall'OTA prima dello swap")
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.selfcheck:
        # Arrivare qui significa che gli import del pacchetto sono andati a buon fine.
        # Costruiamo il transport (senza connettere) per esercitare il nuovo codice.
        try:
            MqttTransport(cfg)
            print(f"selfcheck ok {__version__}")
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"selfcheck FAILED: {e}", file=sys.stderr)
            return 1

    setup_only = False
    if args.enroll:
        parts = (args.enroll.split(":") + [None, None, None])[:3]
        enroll.bootstrap(cfg, parts[0], parts[1], parts[2])
        log.info("enrollment salvato per device=%s", cfg.device_code)
        setup_only = True

    if args.discover_datalogger:
        _discover_datalogger(cfg)
        setup_only = True

    if setup_only:
        return 0

    # inline best-effort discovery before running, if the address is still unknown
    if not cfg.datalogger_ip and cfg.reader_type == "solarlog_getjp":
        _discover_datalogger(cfg)

    if not cfg.is_enrolled():
        log.error("device non enrollato: usa --enroll CODE:SECRET[:STATION_ID]")
        return 2

    reader = build_reader(cfg)
    transport = MqttTransport(cfg)
    buffer = Buffer(cfg.buffer_path, cfg.buffer_max_rows)
    agent = Agent(cfg, reader, transport, buffer)
    try:
        if args.once:
            agent.run_cycle()
        else:
            agent.run_forever()
    finally:
        buffer.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
