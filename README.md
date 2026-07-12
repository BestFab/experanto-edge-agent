# Experanto Edge agent

Open-source agent that runs on a dedicated **Raspberry Pi** (Pi 3 or newer) to turn a
**local datalogger** — today the **Solar-Log Base 2000** — into a remote Experanto source.
The Pi sits on the same LAN as the datalogger, reads it, and forwards the data to
Experanto over **MQTT/TLS**. No inbound ports, no cloud account on the datalogger.

## How it works

```
Solar-Log Base 2000  ──getjp (LAN)──▶  Raspberry Pi (this agent)  ──MQTT/TLS──▶  Experanto
```

- **Thin relay.** The agent forwards the *raw* getjp responses; Experanto parses them
  server-side. Parsing can evolve without updating the fleet.
- **No permanent connection.** The agent connects once per `interval` (default 300s, the
  same rate as every other Experanto worker), publishes telemetry + status, picks up any
  pending command, and disconnects. Offline is detected server-side from stale data.
- **Bidirectional & encrypted.** Telemetry goes up on `experanto/{code}/up/#`; commands
  come down as a retained message on `experanto/{code}/dn/cmd`, all over TLS.
- **Resilient.** Readings that can't be sent are buffered locally (SQLite) and flushed on
  the next successful connect. systemd restarts the agent on failure.

## Install

```bash
git clone https://github.com/Esperanto/experanto-edge-agent
cd experanto-edge-agent
sudo ./install.sh --code EXP-XXXX-XXXX --secret <SECRET> [--datalogger-ip 192.168.1.50]
```

Get the **code** and **secret** from the Experanto SPA → *Add remote datalogger*. If
`--datalogger-ip` is omitted the agent tries to auto-discover the Solar-Log on the LAN.
On the Solar-Log, set the local API access to **Open** so getjp answers.

For the full production setup on a dedicated Pi — the systemd service that **starts on
boot and restarts itself**, configuration reference, remote SSH, updates and
troubleshooting — see **[SETUP.md](SETUP.md)**.

## Run manually

```bash
experanto-edge --enroll EXP-XXXX-XXXX:SECRET:STATION_UUID   # first-time setup
experanto-edge --once                                       # one cycle (debugging)
experanto-edge                                              # loop
```

Config lives at `/etc/experanto-edge/config.yaml` (see `config.example.yaml`).

## Remote commands

Issued from Experanto, delivered on the next cycle:
`read_now`, `set_interval`, `rediscover`, `get_diag`, `restart`, `reboot`,
`update_agent`, `update_system`, `open_ssh`, `close_ssh`.
`open_ssh`/`close_ssh` bring an on-demand WireGuard link (to your self-hosted hub, no third
party) up/down for remote SSH into a Pi behind NAT (see [SETUP.md](SETUP.md) §5 and
[WG_HUB.md](WG_HUB.md)).

## Extending to other buses

Readers are plugins (`experanto_edge/readers/`). New local buses — Modbus TCP/RTU,
RS-485, RS-232, CAN, SunSpec — are added as new readers without touching transport,
enrollment, OTA, or the server. Tracked as separate handoffs.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT — see [LICENSE](LICENSE).
