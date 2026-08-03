"""Reader Azzurro/ZCS Hub (WebSocket locale 55558).

La fixture `fixtures/zcs_status_night.json` e' una risposta `status` REALE dell'hub
(cattura NOTTURNA: 6 inverter Disconnected, INV_REG__DATA vuoti), tagliata ai gruppi
che ci servono. Nessun valore e' inventato.
"""
import json
import socket
import struct
import threading
import time
from pathlib import Path

import pytest

from experanto_edge.config import Config
from experanto_edge.main import READERS, build_reader
from experanto_edge.readers.base import ReaderError
from experanto_edge.readers.zcs_hub_ws import (
    OP_PING,
    OP_PONG,
    OP_TEXT,
    ZcsHubWsReader,
    build_frame,
    inverters_from_scan,
    recv_message,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "zcs_status_night.json").read_text())
STATUS_FRAME_JSON = json.dumps(FIXTURE, separators=(",", ":")).encode()


def deadline(seconds=5.0):
    return time.monotonic() + seconds


def server_frame(opcode, payload=b"", fin=True):
    """Frame server->client: NON mascherato (lo impone l'RFC), lunghezza auto."""
    ln = len(payload)
    b0 = (0x80 if fin else 0x00) | opcode
    if ln < 126:
        hdr = bytes([b0, ln])
    elif ln < 65536:
        hdr = bytes([b0, 126]) + struct.pack(">H", ln)
    else:
        hdr = bytes([b0, 127]) + struct.pack(">Q", ln)
    return hdr + payload


def parse_client_frames(data):
    """Frame client->server (mascherati) -> [(opcode, payload), ...]."""
    out, i = [], 0
    while i + 2 <= len(data):
        b0, b1 = data[i], data[i + 1]
        op, masked, ln = b0 & 0x0F, b1 & 0x80, b1 & 0x7F
        i += 2
        if ln == 126:
            ln = struct.unpack(">H", data[i:i + 2])[0]
            i += 2
        elif ln == 127:
            ln = struct.unpack(">Q", data[i:i + 8])[0]
            i += 8
        key = b""
        if masked:
            key, i = data[i:i + 4], i + 4
        payload, i = data[i:i + ln], i + ln
        if masked:
            payload = bytes(b ^ key[j % 4] for j, b in enumerate(payload))
        out.append((op, payload))
    return out


class FakeSock:
    """Socket finta per i test del codec (nessuna rete)."""

    def __init__(self, data=b""):
        self.inbox = data
        self.sent = b""
        self.timeouts = []

    def recv(self, n):
        chunk, self.inbox = self.inbox[:n], self.inbox[n:]
        return chunk                       # b"" = EOF

    def sendall(self, data):
        self.sent += data

    def settimeout(self, t):
        self.timeouts.append(t)


HANDSHAKE_101 = (b"HTTP/1.1 101 Switching Protocols\r\n"
                 b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n")


class FakeHub:
    """Hub ZCS finto in-process: socket REALE su 127.0.0.1, handshake, frame scriptati.

    Dopo l'invio legge quello che il client ha mandato (stsreq, pong) fino all'EOF,
    cosi' i test possono verificare che il reader NON scriva mai comandi sull'hub.
    """

    def __init__(self, frames, response=HANDSHAKE_101):
        self.frames = frames
        self.response = response
        self.received = []
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(4)
        self._srv.settimeout(0.2)
        self.port = self._srv.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                self._session(conn)
            except OSError:
                pass
            finally:
                conn.close()

    def _session(self, conn):
        conn.settimeout(3.0)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
        conn.sendall(self.response)
        if not self.response.startswith(b"HTTP/1.1 101"):
            return
        for frame in self.frames:
            conn.sendall(frame)
        data = b""
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
        self.received.extend(parse_client_frames(data))

    def wait_received(self, n, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end and len(self.received) < n:
            time.sleep(0.02)
        return self.received

    def stop(self):
        self._stop.set()
        self._srv.close()
        self._thread.join(timeout=3)


@pytest.fixture
def hub():
    made = []

    def make(frames, **kw):
        h = FakeHub(frames, **kw)
        made.append(h)
        return h

    yield make
    for h in made:
        h.stop()


# ---------------- codec dei frame ----------------


@pytest.mark.parametrize("size", [0, 125, 126, 65535, 65536])
def test_frame_roundtrip_and_length_encoding(size):
    payload = bytes((i * 7) % 256 for i in range(size))
    frame = build_frame(OP_TEXT, payload)
    # 2o byte: MASK sempre a 1 (client) + selezione 7bit / 16bit / 64bit.
    length_byte = frame[1] & 0x7F
    assert frame[1] & 0x80                     # client -> server: SEMPRE mascherato
    if size < 126:
        assert length_byte == size
    elif size < 65536:
        assert length_byte == 126 and struct.unpack(">H", frame[2:4])[0] == size
    else:
        assert length_byte == 127 and struct.unpack(">Q", frame[2:10])[0] == size
    if size:                                   # il payload viaggia mascherato, non in chiaro
        assert payload not in frame
    op, msg, rest = recv_message(FakeSock(frame), b"", deadline())
    assert (op, msg, rest) == (OP_TEXT, payload, b"")


def test_continuation_frames_are_reassembled():
    data = (server_frame(OP_TEXT, b'{"head":"sta', fin=False)
            + server_frame(0x0, b'tus","body"', fin=False)
            + server_frame(0x0, b":{}}", fin=True))
    op, msg, _ = recv_message(FakeSock(data), b"", deadline())
    assert op == OP_TEXT
    assert json.loads(msg.decode()) == {"head": "status", "body": {}}


def test_ping_is_answered_with_pong_and_does_not_end_the_message():
    sock = FakeSock(server_frame(OP_PING, b"hi") + server_frame(OP_TEXT, b"ok"))
    op, msg, _ = recv_message(sock, b"", deadline())
    assert (op, msg) == (OP_TEXT, b"ok")       # il ping non interrompe l'attesa
    assert parse_client_frames(sock.sent) == [(OP_PONG, b"hi")]


def test_eof_and_close_frame_are_reader_errors():
    with pytest.raises(ReaderError):
        recv_message(FakeSock(b""), b"", deadline())
    with pytest.raises(ReaderError):
        recv_message(FakeSock(server_frame(0x8, b"\x03\xe8")), b"", deadline())


# ---------------- read() ----------------


def test_read_returns_status_body_verbatim(hub):
    h = hub([
        server_frame(OP_TEXT, b'{"head":"ack","body":{}}'),      # scartato
        server_frame(OP_TEXT, b'{"head":"plot","body":{"x":1}}'),  # scartato
        server_frame(OP_PING, b"beat"),
        server_frame(OP_TEXT, STATUS_FRAME_JSON),
    ])
    reader = ZcsHubWsReader("127.0.0.1", port=h.port, timeout=10)
    out = reader.read()

    assert out["zcs"]["status"] == FIXTURE["body"]        # body VERBATIM, nessun pruning
    assert set(out) == {"read_at", "zcs"}
    assert isinstance(out["read_at"], float)
    assert out["read_at"] > 0
    sent = h.wait_received(2)
    # SOLA LETTURA: l'unico frame applicativo mandato all'hub e' stsreq (+ il pong).
    assert (OP_TEXT, b'{"head":"stsreq"}') in sent
    assert (OP_PONG, b"beat") in sent
    assert [p for op, p in sent if op == OP_TEXT] == [b'{"head":"stsreq"}']


def test_read_returns_a_fresh_dict_each_call(hub):
    # L'agente MUTA il dict di read() (ci aggiunge `discover`): due letture non
    # devono condividere strutture.
    h = hub([server_frame(OP_TEXT, STATUS_FRAME_JSON)])
    reader = ZcsHubWsReader("127.0.0.1", port=h.port, timeout=10)
    first = reader.read()
    second = reader.read()
    assert first is not second
    assert first["zcs"] is not second["zcs"]
    assert first["zcs"]["status"] is not second["zcs"]["status"]
    first["discover"] = {"x": 1}
    first["zcs"]["status"]["STS__PLANT"] = "toccato"
    assert "discover" not in second
    assert second["zcs"]["status"] == FIXTURE["body"]


# ---------------- errori ----------------


def test_closed_port_raises_reader_error():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()                              # porta libera = connessione rifiutata
    with pytest.raises(ReaderError):
        ZcsHubWsReader("127.0.0.1", port=port, timeout=5).read()


def test_handshake_without_101_raises_reader_error(hub):
    h = hub([], response=b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
    with pytest.raises(ReaderError) as e:
        ZcsHubWsReader("127.0.0.1", port=h.port, timeout=5).read()
    assert "403" in str(e.value)


def test_timeout_without_status_raises_reader_error(hub):
    h = hub([server_frame(OP_TEXT, b'{"head":"ack","body":{}}')])   # nessuno status
    reader = ZcsHubWsReader("127.0.0.1", port=h.port, timeout=0.6)
    started = time.monotonic()
    with pytest.raises(ReaderError):
        reader.read()
    assert time.monotonic() - started < 5      # il budget e' rispettato, non appeso


def test_status_without_body_raises_reader_error(hub):
    h = hub([server_frame(OP_TEXT, b'{"head":"status"}')])
    with pytest.raises(ReaderError):
        ZcsHubWsReader("127.0.0.1", port=h.port, timeout=5).read()


def test_no_ip_does_not_crash_at_init_but_read_raises():
    reader = ZcsHubWsReader("")                # hub assente: il costruttore NON solleva
    with pytest.raises(ReaderError):
        reader.read()
    with pytest.raises(ReaderError):
        reader.discover()


# ---------------- discover() ----------------


def test_discover_lists_the_six_real_inverters(hub):
    h = hub([server_frame(OP_TEXT, STATUS_FRAME_JSON)])
    out = ZcsHubWsReader("127.0.0.1", port=h.port, timeout=10).discover()

    assert set(out) == {"read_at", "zcs"}
    assert isinstance(out["read_at"], float)
    inverters = out["zcs"]["inverters"]
    assert len(inverters) == 6                       # 30 slot, 6 occupati
    assert [i["index"] for i in inverters] == [0, 1, 2, 3, 4, 5]
    assert [i["serial"] for i in inverters] == [
        "ZS3ES050N9L261", "ZS3ES050N9L263", "ZS3ES050N9L304",
        "ZS3ES050N9L122", "ZS3ES050N9L303", "ZS3ES050N9L262",
    ]
    assert all(i["status"] == "Disconnected" for i in inverters)   # cattura notturna
    # MODBUS_ADDR e' "" nella cattura reale: dato ASSENTE -> None, mai 0.
    assert all(i["modbus_addr"] is None for i in inverters)


def test_inverters_from_scan_ignores_empty_and_malformed():
    assert inverters_from_scan({}) == []
    assert inverters_from_scan({"STS__INVERTER_SCAN": {"vecStsParams": "boh"}}) == []
    body = {"STS__INVERTER_SCAN": {"vecStsParams": [
        [{"szKey": "INV_SN", "szSV": ""}, {"szKey": "INV_STS", "szSV": "Disconnected"}],
        [{"szKey": "INV_SN", "szSV": "SN-1"}, {"szKey": "INV_STS", "szSV": "Connected"},
         {"szKey": "MODBUS_ADDR", "szSV": "3"}],
    ]}}
    assert inverters_from_scan(body) == [
        {"index": 1, "serial": "SN-1", "status": "Connected", "modbus_addr": "3"}
    ]


# ---------------- registry ----------------


def test_registry_exposes_zcs_hub_ws():
    assert "zcs_hub_ws" in READERS
    cfg = Config(reader_type="zcs_hub_ws", datalogger_ip="192.168.1.107")
    reader = build_reader(cfg)
    assert isinstance(reader, ZcsHubWsReader)
    assert reader.reader_type == "zcs_hub_ws"
    assert reader.ip == "192.168.1.107"
    assert reader.port == 55558                # porta WS fissa, non `datalogger_port` (80)
    # Storico on-demand NON supportato: il default del contratto resta un "no" onesto.
    assert reader.fetch_history_curves(1) is None
