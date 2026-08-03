"""Reader per l'Azzurro/ZCS Hub via il WebSocket LOCALE non autenticato (porta 55558).

Thin relay: chiede lo stato all'hub (`{"head":"stsreq"}`) e forwarda il `body` della
risposta VERBATIM; il parsing dei valori e' interamente server-side, come per il
Solar-Log. Nessun parsing dei valori qui.

Perche' il WebSocket e non il Modbus TCP 55400 documentato: il Modbus dell'hub accetta
UNA SOLA connessione per porta (chi legge esclude tutti gli altri, compreso il cloud
del cliente), mentre il WS espone tutto (~90 grandezze/inverter, MPPT NATIVI) ed e'
multi-client. Provato live sull'hub reale: `stsreq` -> `status` da ~156 KB in 0.1s.

Protocollo osservato (hub firmware 0.2.2.2):
  - handshake WebSocket RFC 6455 standard su ``ws://<ip>:55558/`` (nessun token,
    nessun sottoprotocollo, nessuna origin richiesta);
  - il client manda ``{"head":"stsreq"}`` (frame TEXT);
  - l'hub risponde con messaggi JSON ``{"head": ..., "body": ...}``; oltre a
    ``status`` sulla stessa socket transitano ``plot``/``HP_plot``/``ack``/ping:
    tutto cio' che non e' ``head == "status"`` viene SCARTATO (non e' un errore);
  - il ``body`` di ``status`` e' un dict di gruppi ``STS__*`` (``STS__INVERTER_SCAN``
    = anagrafica inverter, ``STS__INVERTER_REGS`` = registri per inverter, ecc.),
    ognuno con ``vecStsParams`` = lista di gruppi di ``{szKey, szSV}``.

SOLA LETTURA: questo reader non manda MAI scan/cmd/kill_app/cfg_refresh. Lo stesso WS
e' anche il canale di CONTROLLO dell'hub (scrittura su inverter, riavvio applicativo):
resta fuori dallo scopo, la lettura passiva e' l'unica cosa che facciamo.

Zero dipendenze: il client WebSocket e' minimale (socket + struct), derivato dal probe
provato sull'hub. `wsproto`/`websocket-client` non entrano nel Pi per 100 righe.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import socket
import struct
import time
from typing import Any, Dict, List, Optional, Tuple

from .base import Reader, ReaderError

log = logging.getLogger("experanto-edge")

WS_PORT = 55558
STSREQ = b'{"head":"stsreq"}'

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

# Tetto difensivo sulla dimensione di un messaggio riassemblato: lo status reale e'
# ~156 KB, ma un header di lunghezza malformato (64 bit) chiederebbe altrimenti
# un'allocazione illimitata sul Pi.
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


def _xor(payload: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def build_frame(opcode: int, payload: bytes = b"", mask_key: Optional[bytes] = None) -> bytes:
    """Frame RFC 6455 client->server (FIN=1, sempre mascherato come impone lo standard).

    Lunghezza: <126 inline, <65536 su 16 bit (126), altrimenti 64 bit (127).
    `mask_key` esiste per i test (chiave deterministica); in esercizio e' casuale.
    """
    ln = len(payload)
    hdr = bytes([0x80 | opcode])
    if ln < 126:
        hdr += bytes([0x80 | ln])
    elif ln < 65536:
        hdr += bytes([0x80 | 126]) + struct.pack(">H", ln)
    else:
        hdr += bytes([0x80 | 127]) + struct.pack(">Q", ln)
    key = mask_key if mask_key is not None else os.urandom(4)
    return hdr + key + _xor(payload, key)


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise ReaderError("timeout in attesa dell'hub ZCS")
    return left


def _recv_exact(sock: socket.socket, n: int, buf: bytes, deadline: float) -> Tuple[bytes, bytes]:
    while len(buf) < n:
        sock.settimeout(_remaining(deadline))
        chunk = sock.recv(65536)
        if not chunk:
            raise ReaderError("connessione chiusa dall'hub ZCS (EOF)")
        buf += chunk
    return buf[:n], buf[n:]


def recv_message(sock: socket.socket, buf: bytes, deadline: float) -> Tuple[int, bytes, bytes]:
    """Un messaggio applicativo completo -> ``(opcode, payload, buf residuo)``.

    Riassembla i frame di continuation, risponde ai ping con un pong (stesso payload:
    l'hub chiude la socket se non lo vede), ignora i pong. Un frame di close o un EOF
    sono un errore di lettura -> ReaderError.
    """
    msg = b""
    opcode = OP_TEXT
    while True:
        head, buf = _recv_exact(sock, 2, buf, deadline)
        fin, op = head[0] & 0x80, head[0] & 0x0F
        masked, ln = head[1] & 0x80, head[1] & 0x7F
        if ln == 126:
            ext, buf = _recv_exact(sock, 2, buf, deadline)
            ln = struct.unpack(">H", ext)[0]
        elif ln == 127:
            ext, buf = _recv_exact(sock, 8, buf, deadline)
            ln = struct.unpack(">Q", ext)[0]
        if ln > MAX_MESSAGE_BYTES or len(msg) + ln > MAX_MESSAGE_BYTES:
            raise ReaderError(f"frame WebSocket oltre il limite ({ln} byte)")
        key = b""
        if masked:
            key, buf = _recv_exact(sock, 4, buf, deadline)
        payload, buf = _recv_exact(sock, ln, buf, deadline)
        if masked:
            payload = _xor(payload, key)
        if op == OP_PING:
            sock.sendall(build_frame(OP_PONG, payload))
            continue
        if op == OP_CLOSE:
            raise ReaderError("close dall'hub ZCS")
        if op in (OP_CONT, OP_TEXT, OP_BIN):
            if op != OP_CONT:
                opcode = op
            msg += payload
            if fin:
                return opcode, msg, buf
        # pong e opcode ignoti: si scartano, si resta in attesa del messaggio buono


def ws_connect(host: str, port: int, deadline: float) -> Tuple[socket.socket, bytes]:
    """Handshake WebSocket -> ``(socket, byte gia' letti dopo l'header)``.

    L'hub non autentica e non negozia sottoprotocolli: basta l'Upgrade standard.
    Qualunque risposta che non sia 101 e' un errore (mai proseguire "sperando").
    """
    sock = socket.create_connection((host, port), timeout=_remaining(deadline))
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET / HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.settimeout(_remaining(deadline))
        sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            sock.settimeout(_remaining(deadline))
            chunk = sock.recv(4096)
            if not chunk:
                raise ReaderError("handshake WebSocket interrotto (EOF)")
            buf += chunk
            if len(buf) > 65536:
                raise ReaderError("handshake WebSocket: header sproporzionato")
        head, rest = buf.split(b"\r\n\r\n", 1)
        status_line = head.split(b"\r\n", 1)[0]
        if b" 101" not in status_line:
            raise ReaderError(f"handshake WebSocket rifiutato: {status_line[:80]!r}")
        return sock, rest
    except BaseException:
        sock.close()
        raise


def _params_map(group: Any) -> Dict[str, str]:
    """``[{szKey, szSV}, ...]`` -> dict piatto. Voci non conformi ignorate."""
    out: Dict[str, str] = {}
    if not isinstance(group, list):
        return out
    for item in group:
        if isinstance(item, dict) and isinstance(item.get("szKey"), str):
            value = item.get("szSV")
            out[item["szKey"]] = value if isinstance(value, str) else ""
    return out


def inverters_from_scan(body: Any) -> List[Dict[str, Any]]:
    """Anagrafica inverter da ``STS__INVERTER_SCAN`` (slot occupati = INV_SN non vuoto).

    L'hub espone 30 slot fissi: quelli liberi hanno ``INV_SN`` vuoto e vanno esclusi.
    `index` e' la posizione nello scan (la stessa che indicizza STS__INVERTER_REGS).
    Campo vuoto -> None (dato ASSENTE, non zero: di notte MODBUS_ADDR e' "" perche'
    l'inverter non ha ancora parlato, non perche' valga 0).
    """
    scan = body.get("STS__INVERTER_SCAN") if isinstance(body, dict) else None
    groups = scan.get("vecStsParams") if isinstance(scan, dict) else None
    found: List[Dict[str, Any]] = []
    if not isinstance(groups, list):
        return found
    for index, group in enumerate(groups):
        params = _params_map(group)
        serial = (params.get("INV_SN") or "").strip()
        if not serial:
            continue
        found.append({
            "index": index,
            "serial": serial,
            "status": (params.get("INV_STS") or "").strip() or None,
            "modbus_addr": (params.get("MODBUS_ADDR") or "").strip() or None,
        })
    return found


class ZcsHubWsReader(Reader):
    reader_type = "zcs_hub_ws"

    def __init__(self, ip: str, port: int = WS_PORT, timeout: float = 20.0):
        # NON sollevare qui: un hub assente/non ancora configurato non deve far
        # crashare l'agente al boot. L'errore emerge in read() -> lo cattura
        # run_cycle, che riporta lo stato "errore" e ritenta (niente crash-loop).
        self.ip = ip
        self.port = port
        # Budget COMPLESSIVO della lettura (connessione + attesa dello status):
        # oltre, ReaderError. Live lo status arriva in ~0.1s.
        self.timeout = timeout

    # ---- trasporto ----

    def _fetch_status_body(self) -> Dict[str, Any]:
        """`stsreq` -> `body` del primo messaggio ``head == "status"`` (verbatim).

        Chiude SEMPRE la socket. Ogni errore di rete/protocollo/JSON diventa
        ReaderError: e' l'unica eccezione che l'agente cattura.
        """
        if not self.ip:
            raise ReaderError("datalogger_ip non configurato (hub ZCS assente o non ancora impostato)")
        deadline = time.monotonic() + self.timeout
        try:
            sock, buf = ws_connect(self.ip, self.port, deadline)
        except ReaderError:
            raise
        except OSError as e:
            raise ReaderError(f"connessione all'hub ZCS ws://{self.ip}:{self.port} fallita: {e}") from e
        try:
            sock.settimeout(_remaining(deadline))
            sock.sendall(build_frame(OP_TEXT, STSREQ))
            while True:
                opcode, payload, buf = recv_message(sock, buf, deadline)
                if opcode != OP_TEXT:
                    continue                      # binario: l'hub non lo usa per lo status
                try:
                    msg = json.loads(payload.decode("utf-8", errors="replace"))
                except ValueError:
                    continue                      # messaggio non JSON: non e' lo status
                if not isinstance(msg, dict) or msg.get("head") != "status":
                    continue                      # plot / HP_plot / ack / altro: scartato
                body = msg.get("body")
                if not isinstance(body, dict):
                    raise ReaderError("messaggio status dell'hub ZCS senza body")
                return body
        except ReaderError:
            raise
        except (OSError, struct.error) as e:
            raise ReaderError(f"lettura dall'hub ZCS ws://{self.ip}:{self.port} fallita: {e}") from e
        finally:
            try:
                sock.close()
            except OSError:
                pass

    # ---- contratto Reader ----

    def read(self) -> Dict[str, Any]:
        """Snapshot RAW dell'hub. Dict NUOVO a ogni chiamata: l'agente lo muta
        (ci aggiunge `discover`) prima di pubblicarlo."""
        body = self._fetch_status_body()
        return {"read_at": time.time(), "zcs": {"status": body}}

    def discover(self) -> Dict[str, Any]:
        """Inventario inverter: SOLO anagrafica (seriale/indice/stato/indirizzo Modbus),
        nessun valore. Lo status intero e' ~156 KB: la discovery non lo ri-forwarda,
        ci pensa la telemetria."""
        body = self._fetch_status_body()
        return {"read_at": time.time(), "zcs": {"inverters": inverters_from_scan(body)}}

    # `fetch_history_curves`: NON implementato di proposito. Il default None del
    # contratto (base.Reader) = storico on-demand non supportato -> l'agente acka un
    # "senza storico on-demand" onesto invece di inventare una curva.
