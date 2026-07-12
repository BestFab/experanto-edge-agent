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

    # --- remote SSH (reverse tunnel to a self-hosted bastion — no third-party) ---
    # A Pi at a customer site has no inbound ports (often CGNAT). It keeps an OUTBOUND
    # SSH connection to YOUR bastion and exposes its own :22 there with a remote-forward
    # (ssh -R). On `open_ssh` the agent brings the tunnel up for `ssh_default_ttl` seconds,
    # then tears it down (enforced each cycle). Only openssh (+autossh if present) + your VPS.
    ssh_bastion_host: str = ""           # your bastion/VPS hostname or IP
    ssh_bastion_port: int = 22           # sshd port on the bastion
    ssh_bastion_user: str = "edge-tunnel"  # restricted tunnel account on the bastion
    ssh_reverse_port: int = 0            # UNIQUE bastion port forwarding to this Pi (0 = unset)
    ssh_local_port: int = 22             # local sshd port to expose
    ssh_identity: str = "/etc/experanto-edge/tunnel_key"  # private key to auth to the bastion
    ssh_default_ttl: int = 900           # seconds the tunnel stays up per open_ssh

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
