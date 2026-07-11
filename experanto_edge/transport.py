"""MQTT transport — intermittent connections.

The agent connects at each poll cycle, publishes telemetry/status, retrieves a retained
command, and disconnects. MQTT is the substrate on purpose: switching to a persistent
connection later (lower command latency) needs no protocol rewrite.

`Transport` is an abstract interface so the agent loop can be unit-tested with a fake.
`MqttTransport` is the real implementation (paho-mqtt, imported lazily so tests and
tooling don't require the dependency just to import this module).
"""
from __future__ import annotations

import abc
import json
import queue
import ssl
import threading
from typing import Any, Dict, Optional


class TransportError(Exception):
    pass


class Transport(abc.ABC):
    @abc.abstractmethod
    def connect(self) -> None: ...

    @abc.abstractmethod
    def disconnect(self) -> None: ...

    @abc.abstractmethod
    def publish(self, topic: str, payload: Dict[str, Any], qos: int = 1, retain: bool = False) -> bool: ...

    @abc.abstractmethod
    def get_retained_command(self, topic: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]: ...


class MqttTransport(Transport):
    def __init__(self, config):
        self.cfg = config
        self._client = None
        self._connected = threading.Event()
        self._inbox: "queue.Queue" = queue.Queue()

    def connect(self) -> None:
        import paho.mqtt.client as mqtt  # lazy: keep import cost off the test path

        self._connected.clear()
        self._inbox = queue.Queue()
        c = mqtt.Client(client_id=f"edge-{self.cfg.device_code}", clean_session=True)
        c.username_pw_set(self.cfg.device_code, self.cfg.secret)
        if self.cfg.tls:
            c.tls_set(
                ca_certs=self.cfg.ca_cert,
                cert_reqs=ssl.CERT_NONE if self.cfg.tls_insecure else ssl.CERT_REQUIRED,
            )
            if self.cfg.tls_insecure:
                c.tls_insecure_set(True)
        c.on_connect = lambda *_: self._connected.set()
        c.on_message = lambda _c, _u, msg: self._inbox.put(msg)
        # Socket-level failures (ConnectionRefused, DNS, timeout, TLS) all subclass OSError;
        # normalise them to TransportError so run_cycle() can buffer instead of crashing.
        try:
            c.connect(self.cfg.broker_host, self.cfg.broker_port, keepalive=max(30, self.cfg.interval))
            c.loop_start()
            connected = self._connected.wait(self.cfg.connect_timeout)
        except OSError as e:
            try:
                c.loop_stop()
            except Exception:
                pass
            raise TransportError(f"connessione al broker fallita: {e}") from e
        if not connected:
            c.loop_stop()
            raise TransportError("timeout connessione al broker")
        self._client = c

    def disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            finally:
                self._client = None
                self._connected.clear()

    def publish(self, topic, payload, qos=1, retain=False) -> bool:
        if self._client is None:
            raise TransportError("non connesso")
        info = self._client.publish(topic, json.dumps(payload), qos=qos, retain=retain)
        info.wait_for_publish(timeout=self.cfg.connect_timeout)
        return info.is_published()

    def get_retained_command(self, topic, timeout=3.0) -> Optional[Dict[str, Any]]:
        if self._client is None:
            raise TransportError("non connesso")
        self._client.subscribe(topic, qos=1)
        try:
            msg = self._inbox.get(timeout=timeout)
        except queue.Empty:
            return None
        if not msg.payload:
            return None  # empty retained = command cleared
        try:
            return json.loads(msg.payload)
        except ValueError:
            return None
