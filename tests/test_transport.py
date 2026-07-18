"""Real MqttTransport failure semantics.

The agent loop only catches TransportError on connect (main.Agent.run_cycle), so the
real transport MUST raise TransportError — not a bare OSError — when the broker is
unreachable. Otherwise store-and-forward never triggers and the cycle crashes. The
fake in test_main_loop.py can't catch this divergence, hence a test against real paho.
"""
import socket

import pytest

from experanto_edge.config import Config
from experanto_edge.transport import MqttTransport, TransportError


def _closed_port() -> int:
    """A port that is guaranteed closed (bind then release)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_connection_refused_becomes_transport_error():
    cfg = Config(
        device_code="EXP-T",
        secret="s",
        broker_host="127.0.0.1",
        broker_port=_closed_port(),
        tls=False,
        connect_timeout=2,
    )
    with pytest.raises(TransportError):
        MqttTransport(cfg).connect()


def test_unresolvable_host_becomes_transport_error():
    cfg = Config(
        device_code="EXP-T",
        secret="s",
        broker_host="broker.invalid.",  # RFC 6761: never resolves
        broker_port=8883,
        tls=False,
        connect_timeout=2,
    )
    with pytest.raises(TransportError):
        MqttTransport(cfg).connect()
