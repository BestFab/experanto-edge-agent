# Experanto Edge agent

Open-source agent that runs on a **Raspberry Pi** (Pi 3 or newer) to bring a **local
datalogger** — today the **Solar-Log Base** family via its `getjp` API — into
[Experanto](https://experanto.it) over **MQTT/TLS**, and to keep the Pi **remotely
manageable from anywhere** through a self-hosted **WireGuard** overlay. No inbound ports,
no third-party cloud, no cloud account on the datalogger.

```
Solar-Log Base ──getjp (LAN)──▶  Raspberry Pi (this agent)  ──MQTT/TLS──▶  Experanto
                                        │
                                        └──WireGuard (outbound)──▶  your hub  ◀── you: ssh 10.8.0.x
```

## Highlights

- **Thin relay.** Forwards the *raw* getjp responses; Experanto parses them server-side, so
  parsing can evolve without touching the fleet.
- **Intermittent, resilient.** Connects once per `interval` (default 300 s), publishes
  telemetry + status, picks up any pending command, disconnects. Unsendable readings are
  buffered locally (SQLite) and flushed on the next connect. Offline is detected server-side.
- **Reachable behind NAT/CGNAT** via **WireGuard** to *your* hub — a single UDP port that
  coexists with anything else on the host. Survives reboots and network changes. No SaaS.
- **Signed OTA** (Ed25519) with automatic rollback, for both the agent and the OS.
- **Doesn't crash on a missing datalogger** — it starts, reports the error, and retries.
- **Fingerprint an unknown datalogger** with the bundled `tools/recon.py` (stdlib-only).

## Quick start (one-liner)

On the Pi (Raspberry Pi OS **Bookworm**, 64-bit, Pi 3+; the dedicated Pi may be headless):

```bash
curl -fsSL https://github.com/BestFab/experanto-edge-agent/releases/latest/download/install.sh | sudo bash -s -- \
  --code EXP-XXXX-XXXX --secret <SECRET> --station <UUID> \
  --broker mqtt.experanto.it \
  --wg-endpoint <hub-host>:51820 --wg-hub-pubkey <HUB_PUBKEY> --wg-address 10.8.0.5/32 --wg-persistent
```

The installer is **self-bootstrapping**: it downloads the latest release, then sets up a
hardened **systemd** service that starts on boot and restarts itself. Get `--code`/`--secret`
from the Experanto SPA → *Add remote datalogger*. If `--datalogger-ip` is omitted the agent
tries to auto-discover the Solar-Log on the LAN; on the Solar-Log set the local API to **Open**.

Full production runbook (headless image, boot resilience, updates, troubleshooting):
**[SETUP.md](SETUP.md)**.

## Remote access — WireGuard (self-hosted, no third party)

The Pi joins **your** WireGuard hub as a peer with a fixed overlay IP; you SSH straight to
that IP from the hub or from any of your peers, regardless of the Pi's local network:

```bash
ssh <pi-user>@10.8.0.5
```

- On-demand (default): the tunnel is up only for a window on the `open_ssh` command, then down.
- Persistent (`--wg-persistent`): always up — ideal for the initial bring-up and when the
  broker isn't live yet.

Set up the hub in **[WG_HUB.md](WG_HUB.md)**. WireGuard is UDP; if a site blocks *all*
outbound UDP (rare), a TCP reverse-SSH fallback is documented in **[BASTION.md](BASTION.md)**.

## Configuration

Config lives at `/etc/experanto-edge/config.yaml` (see [`config.example.yaml`](config.example.yaml)).

| Group | Keys |
|---|---|
| Identity | `device_code`, `secret`, `station_id` |
| Broker | `broker_host`, `broker_port`, `tls`, `ca_cert`, `tls_insecure` |
| Datalogger | `reader_type`, `datalogger_ip`, `datalogger_port` |
| Behaviour | `interval`, `command_wait`, `buffer_path`, `buffer_max_rows`, `health_path`, `log_level` |
| Remote access | `wg_interface`, `wg_address`, `wg_ssh_user`, `ssh_default_ttl` |
| OTA | `update_base_url`, `update_public_key`, `app_dir`, `ota_helper` |

## Remote commands & OTA

Issued from Experanto, delivered on the next cycle:
`read_now`, `set_interval`, `rediscover`, `get_diag`, `restart`, `reboot`,
`update_agent`, `update_system`, `open_ssh`, `close_ssh`.

- `open_ssh`/`close_ssh` bring the WireGuard link up/down on demand for remote SSH.
- `update_agent` performs a **signed** OTA: the agent verifies the Ed25519 signature + sha256
  of a release, swaps it in atomically, health-checks, and **rolls back** on failure.
  Releases are produced with [`tools/sign_release.py`](tools/sign_release.py).

## Unknown datalogger? Fingerprint it

When you don't know what the device on the other end speaks, run the recon tool on the Pi
(stdlib-only, works over SSH without the venv):

```bash
python3 tools/recon.py            # sweep the LAN + fingerprint
python3 tools/recon.py --json     # machine-readable
```

It identifies HTTP UIs vs JSON APIs and known signatures (Solar-Log getjp, Fronius Solar API,
SMA, Modbus TCP + SunSpec, SNMP) and suggests which reader to use.

## Pre-configured / golden images

Two levels, depending on scale (see **[SETUP.md](SETUP.md)** and the project notes):

- **Level 1 — baked image** (per-device / per-customer): bake the agent + WireGuard into the
  image via `pi-gen`, or boot-once + snapshot + clean per-device state (SSH host keys,
  `machine-id`, WG keys). Simple; one build per device.
- **Level 2 — zero-touch** (one image for all): the device self-generates its identity + a WG
  keypair on first boot and *claims* to an account with a claim-code; the server then issues
  credentials and registers the peer on the hub. Needs a bootstrap/claim endpoint and a WiFi
  provisioning path (ethernet-first, or AP-mode captive portal).

## Extending to other buses

Readers are plugins (`experanto_edge/readers/`). New local buses — Modbus TCP/RTU, RS-485,
RS-232, CAN, SunSpec — are added as new readers without touching transport, enrollment, OTA,
or the server.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q          # unit tests (no hardware, no broker)
```

The core (transport, buffer, OTA, commands, WireGuard) is source-agnostic; the reader is the
only device-specific part. Everything is unit-testable without a Pi, a datalogger, or a broker.

## License

MIT. See [LICENSE](LICENSE).
