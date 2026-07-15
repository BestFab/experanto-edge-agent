"""Configuration loading/saving for the Experanto Edge agent.

The config is a single YAML file (default /etc/experanto-edge/config.yaml). It holds
the device identity (code + secret from enrollment), the broker coordinates, the local
datalogger address, and a little persisted runtime state (interval, last command id).
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import yaml

DEFAULT_CONFIG_PATH = os.environ.get("EXPERANTO_EDGE_CONFIG", "/etc/experanto-edge/config.yaml")


@dataclass
class Config:
    # --- identity / enrollment ---
    device_code: str = ""
    secret: str = ""
    station_id: str = ""

    # --- broker ---
    broker_host: str = "mqtt.experanto.it"
    broker_port: int = 8883
    tls: bool = True
    ca_cert: Optional[str] = None        # path to CA bundle; None = system trust store
    tls_insecure: bool = False           # DEV ONLY: skip certificate verification

    # --- local datalogger ---
    reader_type: str = "solarlog_getjp"
    datalogger_ip: str = ""
    datalogger_port: int = 80
    # Password UTENTE del datalogger (livello "user"). Se impostata, l'agente fa login e
    # legge anche il dettaglio per-inverter (143: temperatura, Udc/Idc/Pdc per stringa, Uac,
    # frequenza) che l'API "open" non espone. Vuota = solo dati open (nessuna regressione).
    # Raccolta dal wizard/console per install; per datalogger senza password resta vuota.
    datalogger_user_password: str = ""
    # Raccolta del dettaglio per-inverter (getjp 143: temperatura/Udc/Idc/Pdc/Uac/
    # frequenza). OFF di default: il 143 scarica l'intera giornata per inverter e il
    # mapping colonne richiede i blocchi 860/870, che sul firmware attuale rispondono
    # 503 (dato inutilizzabile lato server). Abilitare a `true` solo quando 860/870
    # tornano disponibili e il mapping server e' pronto.
    collect_inverter_detail: bool = False

    # --- behaviour ---
    interval: int = 300                  # seconds between cycles (= worker poll rate)
    connect_timeout: int = 20
    command_wait: float = 3.0            # seconds to wait for a retained command per cycle
    buffer_path: str = "/var/lib/experanto-edge/buffer.db"
    buffer_max_rows: int = 5000
    log_level: str = "INFO"
    health_path: str = "/var/lib/experanto-edge/health"  # touched each cycle; OTA rollback watches it

    # --- OTA (phase E5) ---
    # Releases are signed server-side (Ed25519) and served under update_base_url as
    #   experanto-edge-{version}.tar.gz  +  experanto-edge-{version}.json  (manifest)
    # The agent verifies sha256 + signature against update_public_key, then hands the
    # privileged install/swap/restart to a root helper (ota_helper) via sudo -n.
    update_base_url: str = ""            # e.g. https://mqtt.experanto.it/releases
    update_public_key: str = ""         # base64 of the raw 32-byte Ed25519 public key
    app_dir: str = "/opt/experanto-edge"          # holds current -> releases/{version}
    ota_helper: str = "/opt/experanto-edge/ota-helper.sh"

    # --- remote access (WireGuard to a self-hosted hub — no third-party) ---
    # A Pi at a customer site has no inbound ports (often CGNAT). It joins YOUR WireGuard hub
    # (one UDP port on your VPS — coexists with nginx/sshd, even UDP/443) as a peer with a
    # fixed overlay IP; you SSH straight to that IP. On `open_ssh` the agent brings the WG
    # interface up for `ssh_default_ttl` seconds, then tears it down (enforced each cycle).
    # Keys/endpoint/peer live in /etc/wireguard/<iface>.conf; here we only need iface + IP.
    wg_interface: str = "wg-experanto"   # name of /etc/wireguard/<iface>.conf
    wg_address: str = ""                 # this Pi's overlay IP, e.g. 10.8.0.5/32 (per device)
    wg_ssh_user: str = ""                # login user shown in the reach hint (optional)
    ssh_default_ttl: int = 900           # seconds the tunnel stays up per open_ssh
    # Chi possiede l'interfaccia WireGuard. True (DEFAULT) = l'overlay e' PERSISTENTE e
    # gestito dal sistema (es. `wg-quick@<iface>` abilitato al boot): e' la LIFELINE del Pi
    # e l'agente NON deve mai toccarla -> open_ssh/close_ssh diventano no-op sull'interfaccia
    # (resta comunque su, il Pi resta raggiungibile). Cosi' agente-dati e connettivita'-WG
    # sono INDIPENDENTI: un deploy dell'agente non puo' abbattere la lifeline.
    # Mettere False SOLO su Pi dove l'agente possiede un'interfaccia on-demand (nessun
    # overlay persistente): li' l'agente la porta su/giu' a finestra (comportamento storico).
    wg_managed_externally: bool = True

    # --- persisted runtime state ---
    last_command_id: str = ""
    ssh_open_until: int = 0              # epoch until which the SSH tunnel stays up (0 = closed)

    _path: str = field(default=DEFAULT_CONFIG_PATH, repr=False)

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        p = path or DEFAULT_CONFIG_PATH
        data = {}
        if Path(p).exists():
            with open(p) as f:
                data = yaml.safe_load(f) or {}
        known = {k for k in cls.__dataclass_fields__ if not k.startswith("_")}
        cfg = cls(**{k: v for k, v in data.items() if k in known})
        cfg._path = p
        return cfg

    def save(self, path: Optional[str] = None) -> None:
        p = path or self._path
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        data = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        tmp = f"{p}.tmp"
        with open(tmp, "w") as f:
            yaml.safe_dump(data, f, sort_keys=True)
        os.replace(tmp, p)  # atomic swap

    def topic(self, leaf: str) -> str:
        """Per-device topic, e.g. experanto/{code}/up/telemetry."""
        return f"experanto/{self.device_code}/{leaf}"

    def is_enrolled(self) -> bool:
        return bool(self.device_code and self.secret)
