#!/usr/bin/env python3
"""recon.py — identifica un datalogger SCONOSCIUTO sulla LAN, da eseguire SUL Pi.

Nessuna dipendenza (solo stdlib): gira su un Raspberry Pi appena installato, via SSH,
senza venv né pip. Utile quando il Pi è a distanza e non sai cosa c'è dall'altra parte
del cavo ethernet.

Fa, in ordine:
  1. scopre gli host attivi sulla /24 locale (o su un IP/subnet passato);
  2. port-scan dei servizi tipici di un datalogger/inverter;
  3. fingerprint HTTP (UI web vs API JSON: Server, Content-Type, <title>, auth);
  4. firme note: Solar-Log getjp, Fronius Solar API, SMA, Modbus TCP + SunSpec, SNMP;
  5. per ogni host un VERDETTO + il prossimo passo suggerito (quale reader).

Uso:
    python3 recon.py                  # scopre e sonda la /24 locale
    python3 recon.py 192.168.1.50     # sonda un IP specifico
    python3 recon.py 192.168.1.0/24   # sonda una subnet
    python3 recon.py --json           # output JSON (incollalo in chat per decidere insieme)
"""
from __future__ import annotations

import argparse
import concurrent.futures
import ipaddress
import json
import re
import socket
import ssl
import struct
import sys

LIVENESS_PORTS = [80, 443, 502, 22, 8080]           # per capire se un host è vivo
SCAN_PORTS = [22, 23, 80, 443, 502, 1883, 8080, 8081, 8443, 9090]
HTTP_PORTS = [(80, False), (8080, False), (8081, False), (443, True), (8443, True)]


# --------------------------------------------------------------------------- net
def local_subnet():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ipaddress.ip_network(f"{ip}/24", strict=False)
    except Exception:
        return None


def tcp_open(ip, port, timeout=0.5):
    try:
        with socket.create_connection((str(ip), port), timeout=timeout):
            return True
    except Exception:
        return False


def sweep(net, timeout=0.4, workers=128):
    hosts = list(net.hosts())

    def probe(ip):
        return str(ip) if any(tcp_open(ip, p, timeout) for p in LIVENESS_PORTS) else None

    live = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(probe, hosts):
            if r:
                live.append(r)
    return sorted(live, key=lambda x: tuple(int(o) for o in x.split(".")))


def scan_ports(ip, timeout=0.6, workers=16):
    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(tcp_open, ip, p, timeout): p for p in SCAN_PORTS}
        for f in concurrent.futures.as_completed(futs):
            if f.result():
                found.append(futs[f])
    return sorted(found)


# --------------------------------------------------------------------------- http
def _http_raw(ip, port, tls, path="/", method="GET", body=None, headers=None, timeout=4):
    raw = socket.create_connection((ip, port), timeout=timeout)
    sock = raw
    if tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        sock = ctx.wrap_socket(raw, server_hostname=ip)
    try:
        req = (f"{method} {path} HTTP/1.1\r\nHost: {ip}\r\nConnection: close\r\n"
               f"User-Agent: experanto-recon\r\nAccept: */*\r\n")
        if headers:
            for k, v in headers.items():
                req += f"{k}: {v}\r\n"
        payload = b""
        if body is not None:
            payload = body.encode() if isinstance(body, str) else body
            req += f"Content-Length: {len(payload)}\r\n"
        req += "\r\n"
        sock.sendall(req.encode() + payload)
        sock.settimeout(timeout)
        chunks, total = [], 0
        while total < 16384:
            try:
                d = sock.recv(4096)
            except Exception:
                break
            if not d:
                break
            chunks.append(d)
            total += len(d)
        data = b"".join(chunks)
    finally:
        try:
            sock.close()
        except Exception:
            pass
    head, _, body_b = data.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1", "replace").split("\r\n")
    status = lines[0] if lines else ""
    hdrs = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            hdrs[k.strip().lower()] = v.strip()
    return status, hdrs, body_b


def http_probe(ip, port, tls):
    try:
        status, hdrs, body = _http_raw(ip, port, tls)
    except Exception:
        return None
    title = ""
    m = re.search(rb"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    if m:
        title = m.group(1).decode("utf-8", "replace").strip()[:120]
    ctype = hdrs.get("content-type", "")
    head = body[:400].lstrip().lower()
    is_json = "json" in ctype or body[:200].lstrip()[:1] in (b"{", b"[")
    is_html = "html" in ctype or head.startswith(b"<!doctype html") or b"<html" in head
    return {
        "port": port, "tls": tls, "status": status.replace("HTTP/1.1 ", "").replace("HTTP/1.0 ", ""),
        "server": hdrs.get("server", ""), "content_type": ctype,
        "location": hdrs.get("location", ""), "title": title,
        "is_json": bool(is_json), "is_html": bool(is_html),
        "auth": hdrs.get("www-authenticate", ""),
    }


# ---------------------------------------------------------------- solar signatures
def probe_getjp(ip):
    for port in (80, 8080):
        try:
            status, _, body = _http_raw(ip, port, False, "/getjp", "POST",
                                        '{"801":{"170":null}}', {"Content-Type": "application/json"})
            if "200" in status and body.lstrip()[:1] == b"{":
                return port
        except Exception:
            pass
    return None


def probe_fronius(ip):
    try:
        status, _, body = _http_raw(ip, 80, False, "/solar_api/GetAPIVersion.cgi")
        if "200" in status and b"BaseURL" in body:
            try:
                return json.loads(body.decode("utf-8", "replace"))
            except Exception:
                return {"BaseURL": "?"}
    except Exception:
        pass
    return None


def probe_sma(ip):
    for port, tls in ((443, True), (80, False)):
        try:
            _, _, body = _http_raw(ip, port, tls, "/dyn/login.json", "POST",
                                   '{"right":"usr","pass":"x"}', {"Content-Type": "application/json"})
            if b"result" in body or b"\"err\"" in body:
                return port
        except Exception:
            pass
    return None


# ------------------------------------------------------------------ Modbus/SunSpec
def modbus_read(ip, unit, addr, qty, timeout=3):
    pdu = struct.pack(">BHH", 3, addr, qty)                 # func 3 = read holding regs
    mbap = struct.pack(">HHHB", 1, 0, len(pdu) + 1, unit)
    try:
        with socket.create_connection((ip, 502), timeout=timeout) as s:
            s.sendall(mbap + pdu)
            s.settimeout(timeout)
            resp = s.recv(512)
    except Exception:
        return None
    if len(resp) < 9:
        return None
    func = resp[7]
    if func == 3:
        bc = resp[8]
        return {"data": resp[9:9 + bc]}
    if func == 0x83:
        return {"exception": resp[8] if len(resp) > 8 else None}
    return {"raw": resp[:12].hex()}


def modbus_probe(ip):
    base = modbus_read(ip, 1, 0, 2) or modbus_read(ip, 3, 0, 2) or modbus_read(ip, 0, 0, 2)
    if not base:
        return None
    out = {"present": True, "sunspec": False}
    for a in (40000, 50000, 0):                            # marker "SunS" (0x53756E53)
        r = modbus_read(ip, 1, a, 2)
        if r and r.get("data", b"")[:4] == b"SunS":
            out.update(sunspec=True, sunspec_base=a)
            break
    return out


# --------------------------------------------------------------------- SNMP (hint)
def _ber_len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag, val):
    return bytes([tag]) + _ber_len(len(val)) + val


def snmp_sysdescr(ip, community="public", timeout=1.5):
    """Best-effort SNMPv2c GET di sysDescr.0 (1.3.6.1.2.1.1.1.0). None se muto."""
    oid = bytes([0x2b, 6, 1, 2, 1, 1, 1, 0])
    varbind = _tlv(0x30, _tlv(0x06, oid) + _tlv(0x05, b""))
    pdu = _tlv(0xA0, _tlv(0x02, b"\x01") + _tlv(0x02, b"\x00") +   # req-id=1, err=0
               _tlv(0x02, b"\x00") + _tlv(0x30, varbind))          # err-idx=0, varbinds
    msg = _tlv(0x30, _tlv(0x02, b"\x01") + _tlv(0x04, community.encode()) + pdu)  # v2c
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        s.sendto(msg, (ip, 161))
        resp, _ = s.recvfrom(2048)
        s.close()
    except Exception:
        return None
    # euristica: prendi la più lunga OCTET STRING stampabile (≠ community)
    best = ""
    i = 0
    while i < len(resp) - 2:
        if resp[i] == 0x04:
            ln = resp[i + 1]
            if ln < 0x80:
                val = resp[i + 2:i + 2 + ln]
                txt = "".join(chr(b) for b in val if 32 <= b < 127)
                if len(txt) > len(best) and txt != community:
                    best = txt
                i += 2 + ln
                continue
        i += 1
    return best[:160] or None


# ------------------------------------------------------------------------- verdict
def analyse(ip):
    r = {"ip": ip, "ports": scan_ports(ip)}
    r["http"] = [h for p, tls in HTTP_PORTS if (h := http_probe(ip, p, tls))] if any(
        p in r["ports"] for p, _ in HTTP_PORTS) else []
    r["getjp"] = probe_getjp(ip) if (80 in r["ports"] or 8080 in r["ports"]) else None
    r["fronius"] = probe_fronius(ip) if 80 in r["ports"] else None
    r["sma"] = probe_sma(ip) if (443 in r["ports"] or 80 in r["ports"]) else None
    r["modbus"] = modbus_probe(ip) if 502 in r["ports"] else None
    r["snmp"] = snmp_sysdescr(ip)
    r["verdict"] = verdict(r)
    return r


def verdict(r):
    v = []
    if r.get("getjp"):
        v.append(f"Solar-Log getjp JSON su :{r['getjp']} → reader `solarlog_getjp` (GIÀ supportato).")
    if r.get("fronius"):
        v.append("Fronius Solar API locale (JSON su /solar_api) → reader tipo Fronius.")
    if r.get("sma"):
        v.append(f"SMA (endpoint /dyn su :{r['sma']}) → reader SMA (JSON dietro login).")
    mb = r.get("modbus")
    if mb and mb.get("sunspec"):
        v.append(f"Modbus TCP + SunSpec su :502 (base reg {mb.get('sunspec_base')}) → reader Modbus/SunSpec: integrazione STANDARD, la via consigliata.")
    elif mb and mb.get("present"):
        v.append("Modbus TCP su :502 ma niente marker SunSpec → mappa registri proprietaria: serve la doc del costruttore.")
    for h in r.get("http", []):
        if h["is_json"] and not (r.get("getjp") or r.get("fronius") or r.get("sma")):
            v.append(f"API JSON non riconosciuta su :{h['port']} ({h.get('content_type')}) → ispeziona gli endpoint (paths dalla UI / doc).")
        elif h["is_html"] and not mb:
            who = f" ({h['title']})" if h["title"] else ""
            v.append(f"Solo UI web HTML su :{h['port']}{who} → o c'è un'API nascosta (guarda le XHR della pagina), oppure serve scraping (reader 'fat' che parsa sul Pi).")
    if r.get("snmp"):
        v.append(f"SNMP sysDescr: “{r['snmp']}” (indizio sul modello).")
    if not v:
        opened = ", ".join(str(p) for p in r["ports"]) or "nessuna"
        v.append(f"Nessuna firma nota. Porte aperte: {opened}. Prova quelle a mano (telnet/curl) o è dietro un altro switch/VLAN.")
    return v


# ---------------------------------------------------------------------------- main
def targets_from_arg(arg):
    if not arg:
        net = local_subnet()
        if not net:
            print("Non riesco a determinare la subnet locale; passa un IP o CIDR.", file=sys.stderr)
            sys.exit(2)
        print(f"[recon] scopro host attivi su {net} …", file=sys.stderr)
        hosts = sweep(net)
        print(f"[recon] host attivi: {', '.join(hosts) or 'nessuno'}", file=sys.stderr)
        return hosts
    if "/" in arg:
        return sweep(ipaddress.ip_network(arg, strict=False))
    return [arg]


def print_report(results):
    for r in results:
        print("=" * 68)
        print(f"HOST {r['ip']}   porte aperte: {', '.join(map(str, r['ports'])) or '—'}")
        for h in r.get("http", []):
            tag = "JSON" if h["is_json"] else "HTML" if h["is_html"] else "?"
            extra = f" · {h['title']}" if h["title"] else ""
            srv = f" · Server: {h['server']}" if h["server"] else ""
            auth = " · AUTH richiesta" if h["auth"] else ""
            print(f"  http :{h['port']}{'(tls)' if h['tls'] else ''} → {h['status']} [{tag}] {h['content_type']}{srv}{extra}{auth}")
        if r.get("modbus"):
            mb = r["modbus"]
            print(f"  modbus :502 → presente{' + SunSpec' if mb.get('sunspec') else ''}")
        if r.get("snmp"):
            print(f"  snmp :161 → {r['snmp']}")
        print("  VERDETTO:")
        for line in r["verdict"]:
            print(f"   • {line}")
    print("=" * 68)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fingerprint di un datalogger sconosciuto sulla LAN.")
    ap.add_argument("target", nargs="?", help="IP, CIDR, o vuoto per la /24 locale")
    ap.add_argument("--json", action="store_true", help="output JSON (per incollarlo in chat)")
    args = ap.parse_args(argv)

    hosts = targets_from_arg(args.target)
    if not hosts:
        print("Nessun host da sondare.", file=sys.stderr)
        return 1
    results = [analyse(ip) for ip in hosts]
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print_report(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
