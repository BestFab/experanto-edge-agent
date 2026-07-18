"""Experanto Edge agent — entry point and cycle loop.

Each cycle (every `interval` seconds): read the local datalogger, connect to the broker,
flush any buffered readings, publish telemetry + status, pick up one retained command,
ack it, and disconnect. No permanent connection is held — offline is detected server-side
from stale telemetry, exactly like any other plant.

Con `persistent_commands=True` (opt-in) l'agente tiene invece una connessione MQTT
persistente: i comandi arrivano istantaneamente (necessario allo storico on-demand, che
il server aspetta ~22s), la telemetria resta sul timer `interval`. Vedi `_run_persistent`.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time

from . import __version__, enroll, remote, update
from .buffer import Buffer
from .commands import CommandDispatcher
from .config import Config
from .readers.base import Reader, ReaderError
from .readers.solarlog_getjp import SolarlogGetjpReader
from .transport import MqttTransport, Transport, TransportError

log = logging.getLogger("experanto-edge")

# reader_type -> factory. Future buses (modbus/rs485/can/...) register here (phase: handoff).
READERS = {
    "solarlog_getjp": lambda cfg: SolarlogGetjpReader(
        cfg.datalogger_ip, cfg.datalogger_port,
        user_password=cfg.datalogger_user_password,
        collect_detail=cfg.collect_inverter_detail,
    ),
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

    def open_ssh(self, args: dict):
        """Bring the WireGuard tunnel up for a bounded window so we can SSH in.
        `args` may carry {ttl} to override the default window."""
        ttl = max(60, min(int(args.get("ttl") or self.cfg.ssh_default_ttl), 86400))
        ok, info = remote.up(self.cfg, ttl)
        if not ok:
            return False, info  # info is the failure reason (str)
        self.cfg.ssh_open_until = int(time.time()) + ttl
        self.cfg.save()
        log.info("tunnel WireGuard aperto per %ss (%s)", ttl, info.get("reach"))
        return True, json.dumps(
            {"reach": info.get("reach"), "address": info.get("address"),
             "until": self.cfg.ssh_open_until}
        )

    def close_ssh(self, args: dict):
        remote.down(self.cfg)
        self.cfg.ssh_open_until = 0
        self.cfg.save()
        return True, "tunnel SSH chiuso"

    def _enforce_ssh_window(self) -> None:
        """Tear the tunnel down once its window elapses (on-demand, never always-on).
        Persisted in config so it survives a restart; granularity is one cycle."""
        if self.cfg.ssh_open_until and time.time() > self.cfg.ssh_open_until:
            remote.down(self.cfg)
            self.cfg.ssh_open_until = 0
            self.cfg.save()
            log.info("finestra SSH scaduta — tunnel chiuso")

    def fetch_history(self, args: dict, cmd: dict):
        """On-demand: curva per-inverter STORICA -> chunk `up/history` (uno per device).

        Thin-relay: il reader forwarda il RAW (143:100 + 860), il server ricostruisce
        la curva potenza. UN device per chunk (curva ~90KB, cap gateway 256KB -> mai
        bundlare >=2); il 860 (piccolo, statico) va in ogni chunk cosi' il server
        parsa ciascuno stateless. La correlazione e' via `command_id` (l'ack resta
        piccolo, il payload NON ci va). `args`: {date, daysback}. Brand-agnostico:
        richiede solo che il reader esponga `fetch_history_curves`."""
        fetch = getattr(self.reader, "fetch_history_curves", None)
        if not callable(fetch):
            return False, f"reader {self.cfg.reader_type} senza storico on-demand"
        try:
            daysback = int(args.get("daysback"))
        except (TypeError, ValueError):
            return False, "daysback mancante/non valido"
        if daysback < 0:
            return False, "daysback negativo"
        date = str(args.get("date") or "")
        command_id = cmd.get("command_id")
        res = fetch(daysback) or {}
        ch860 = res.get("ch860")
        curves = res.get("curves") or {}
        total = len(curves)
        sent = 0
        for idx, node in curves.items():
            payload = {
                "schema": "experanto.edge.history/1",
                "device_code": self.cfg.device_code,
                "command_id": command_id,
                "device_idx": int(idx),
                "date": date,
                "total_devices": total,
                "raw": {"143": {str(idx): {"100": {str(daysback): node}}}, "860": ch860},
            }
            if self.transport.publish(self.cfg.topic("up/history"), payload):
                sent += 1
        return True, json.dumps({"done": True, "n_devices": total, "sent": sent})

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
                "ssh_open_until": self.cfg.ssh_open_until,
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
    def _read_telemetry(self):
        """Legge il datalogger -> (telemetry|None, error).

        Un errore del reader NON alza: la telemetria resta None e lo status riporta
        l'errore. Condiviso dal ciclo intermittente e da quello persistente."""
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
        return telemetry, error

    def run_cycle(self) -> None:
        self._enforce_ssh_window()
        telemetry, error = self._read_telemetry()
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
            "avvio agente %s device=%s interval=%ss%s",
            __version__, self.cfg.device_code, self.cfg.interval,
            " [persistent]" if self.cfg.persistent_commands else "",
        )
        update.write_health_marker(self.cfg)  # signal "process is up" ASAP (OTA rollback watches it)
        if self.cfg.persistent_commands:
            self._run_persistent()
        else:
            self._run_intermittent()
        log.info("arresto agente")

    def _run_intermittent(self) -> None:
        """Modello storico (default): ogni `interval` connette, pubblica, legge UN
        comando, disconnette. Latenza comando fino a `interval`. Path provato."""
        while not self._stop.is_set():
            start = time.time()
            try:
                self.run_cycle()
            except Exception:
                log.exception("errore nel ciclo")
            update.write_health_marker(self.cfg)
            self._wake.clear()
            self._wake.wait(max(0.0, self.cfg.interval - (time.time() - start)))

    def _run_persistent(self) -> None:
        """Connessione persistente: comandi ISTANTANEI (coda inbound drenata nel thread
        principale — nessuna concorrenza sul datalogger), telemetria sul timer
        `interval`. paho riconnette da solo; se la connessione iniziale fallisce
        bufferizziamo e ritentiamo. WireGuard resta indipendente (verso il broker)."""
        try:
            self.transport.connect(persistent=True)
            self.transport.subscribe(self.cfg.topic("dn/cmd"))
        except TransportError as e:
            log.warning("connessione persistente iniziale fallita: %s (ritento)", e)
        next_telemetry = 0.0
        while not self._stop.is_set():
            if time.time() >= next_telemetry:
                try:
                    self._publish_persistent()
                except Exception:
                    log.exception("errore nel ciclo telemetria")
                update.write_health_marker(self.cfg)
                next_telemetry = time.time() + self.cfg.interval
            # Attesa comando: cap a 5s per rivalutare _stop (shutdown rapido) e il timer;
            # un comando in coda torna SUBITO (queue.get non aspetta il cap).
            wait = max(0.1, min(next_telemetry - time.time(), 5.0))
            cmd = self.transport.next_command(wait)
            if cmd:
                try:
                    self._dispatch_persistent(cmd)
                except Exception:
                    # un comando (o un cfg.save fallito) non deve MAI fermare il loop
                    log.exception("errore gestendo comando")
                if self._wake.is_set():          # read_now/rediscover -> lettura immediata
                    self._wake.clear()
                    next_telemetry = 0.0
        self.transport.disconnect()

    def _publish_persistent(self) -> None:
        """Legge + pubblica telemetria/status sulla connessione persistente (niente
        connect/disconnect per ciclo). Broker giu' (paho in reconnect) -> bufferizza."""
        self._enforce_ssh_window()
        telemetry, error = self._read_telemetry()
        if not self.transport.connected():
            if telemetry:
                self.buffer.append(self.cfg.topic("up/telemetry"), telemetry)
            return
        self._flush_buffer()
        if telemetry and not self.transport.publish(self.cfg.topic("up/telemetry"), telemetry):
            self.buffer.append(self.cfg.topic("up/telemetry"), telemetry)
        self.transport.publish(self.cfg.topic("up/status"), self._status(error))

    def _dispatch_persistent(self, cmd: dict) -> None:
        """Esegue un comando arrivato via on_message (dedup per command_id, poi ack)."""
        cid = cmd.get("command_id")
        if cid and cid == self.cfg.last_command_id:
            return  # gia' gestito (il retained persiste finche' non ripulito)
        ok, detail = self.dispatcher.handle(cmd)
        if self.transport.connected():
            self.transport.publish(self.cfg.topic("up/ack"), self._ack(cmd, ok, detail))
        if cid:
            self.cfg.last_command_id = cid
            self.cfg.save()


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
