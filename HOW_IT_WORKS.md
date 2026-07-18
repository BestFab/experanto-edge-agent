# How the Experanto Edge agent works

This explains **what the agent actually does at runtime** and how the pieces fit together —
the level below the [README](README.md) (which is install/config) and above the source.

## 1. Big picture

The agent is a small, long-running Python process on a Raspberry Pi. Once per interval it reads a
local datalogger and ships the reading to Experanto over MQTT/TLS. It holds **no permanent
connection**: it connects, publishes, picks up any pending command, and disconnects. A separate,
outbound **WireGuard** overlay keeps the Pi reachable for SSH/administration.

```
                    ┌───────────────── Raspberry Pi (this agent) ─────────────────┐
 Solar-Log Base ──▶ │  reader.read() ──▶ telemetry ──▶ transport(MQTT/TLS) ──▶    │ ──▶ Experanto
   getjp (LAN)      │        │                  │                                 │      (broker)
                    │        │            buffer(SQLite) ◀── on failure           │
                    │        └── status, ack ─────────────────────────────────▶  │
                    └───────────────────────────┬─────────────────────────────────┘
                                                 └── WireGuard (outbound) ──▶ your hub ◀── you: ssh 10.8.0.x
```

Two independent planes: the **data plane** (reader → MQTT) and the **connectivity plane**
(WireGuard). They do not depend on each other — the agent can be updated or fail without affecting
the overlay, and vice-versa (see §7).

## 2. Lifecycle

1. **Boot.** `systemd` starts `experanto-edge.service` (`Restart=always`). One Pi can run several
   instances, one per datalogger, each with its own config file and SQLite buffer.
2. **Config.** `config.py` loads `/etc/experanto-edge/config.yaml` (path overridable via
   `EXPERANTO_EDGE_CONFIG`). Unknown keys are ignored; a little runtime state (`interval`,
   `last_command_id`, `ssh_open_until`) is persisted back atomically.
3. **Wire-up.** `main.py` builds the reader from `reader_type` (the `READERS` factory), the MQTT
   transport, and the buffer, then enters the cycle loop — running `run_cycle()` every `interval`
   seconds (default 300). The loop can be woken early by a command (`read_now`) or a discovery
   request.

## 3. The read cycle — `Agent.run_cycle()`

This is the heart. Each cycle, in order:

1. **Enforce the SSH window** — if a bounded `open_ssh` window has elapsed, close it (a no-op on a
   system-managed overlay; see §7).
2. **Read the datalogger** — `reader.read()` returns the raw getjp blocks. On error it's caught:
   `telemetry` stays `None` and the error goes into the status message (the agent never crashes on a
   bad read).
3. **Build telemetry** — wrap the reading in the `experanto.edge.telemetry/1` envelope
   (`device_code`, `station_id`, `reader_type`, `agent_version`, `read_at`, `data`).
4. **Connect** to the broker. If it's unreachable → **buffer** the telemetry to SQLite and return
   (store-and-forward). Nothing is lost.
5. **Flush the buffer** — publish anything held from previous offline cycles, stopping at the first
   failure (keep the rest for next time).
6. **Publish telemetry** — and if the publish fails, buffer it.
7. **Publish status** — diagnostics (version, OS, uptime, buffered count, interval), `local_ips`,
   `ssh_open_until`, and any read error.
8. **Handle one command** — read a single *retained* command from `dn/cmd`, skip it if already done
   (dedup by `last_command_id`), dispatch it, and publish an **ack**.
9. **Disconnect** (always, in `finally`).

Because the connection is per-cycle, **"offline" is inferred server-side** from stale telemetry —
exactly like any other plant. There's no heartbeat to keep alive.

## 4. Messages & topics

All under `experanto/{device_code}/`:

| Topic | Direction | Payload (schema) |
|---|---|---|
| `up/telemetry` | Pi → server | `experanto.edge.telemetry/1` — the raw reading under `data` |
| `up/status`    | Pi → server | `experanto.edge.status/1` — diagnostics + error + `ssh_open_until` |
| `up/ack`       | Pi → server | `experanto.edge.ack/1` — result of a command |
| `dn/cmd`       | server → Pi | one **retained** command, delivered on the next cycle |

Commands: `read_now`, `set_interval`, `rediscover`, `get_diag`, `restart`, `reboot`,
`update_agent`, `update_system`, `open_ssh`, `close_ssh`.

## 5. The reader — thin relay

The reader is the only device-specific part. The Solar-Log reader (`readers/solarlog_getjp.py`)
forwards the **raw** getjp responses; Experanto parses the numeric indices **server-side**, so
parsing can evolve without re-flashing the fleet. Per cycle it fetches, best-effort and spaced out
(the Base returns 503 if hammered):

- `801/170` — plant aggregate (Pac, Pdc, Uac/Udc, yield today/yesterday/total, installed Wp)
- `782` — per-inverter AC power · `608` — per-inverter status · `740` — per-inverter serials
- `877`/`878` — monthly/yearly history (throttled; changes slowly)
- `143` — per-inverter intraday detail (temperature, per-string Udc/Idc/Pdc, Uac, frequency),
  **only if `collect_inverter_detail` is on**, and only for the real inverters (from `740`)

Only `801/170` and `782` are required; everything else degrades gracefully if a block is missing.
Requires the Solar-Log's local API set to **Open**. New buses (Modbus, SunSpec, …) are added as new
readers under `readers/` without touching transport, buffer, OTA, or the server (see README →
*Extending to other buses*).

## 6. Resilience

- **Store-and-forward** — offline readings go to SQLite (`buffer_path`, capped at `buffer_max_rows`)
  and flush in order on reconnect.
- **No crash on a bad datalogger** — a read error is reported in status and retried next cycle.
- **Anti-503 spacing** — deliberate delay between getjp queries.
- **`health_path`** is touched each cycle; the OTA rollback watches it.

## 7. Remote access — WireGuard (two independent modes)

The Pi joins *your* self-hosted WireGuard hub as a peer with a fixed overlay IP; you SSH straight to
it. There are two modes, and **the choice is what keeps the data agent and connectivity independent**:

- **Externally managed (default, `wg_managed_externally=True`)** — the overlay is a **persistent
  system service** (`wg-quick@<iface>` enabled at boot). It is the Pi's lifeline, and the agent
  **never touches it**: `open_ssh` just reports how to reach the Pi, `close_ssh` and window-expiry
  are no-ops. So no agent deploy, refactor, or command can ever take the Pi offline.
- **Agent managed (`wg_managed_externally=False`)** — for a Pi with *no* persistent overlay (pure
  on-demand): the agent brings the interface **up** for a bounded window on `open_ssh` and **down**
  when it expires. Only safe when the agent exclusively owns that interface.

`wg-quick` needs root; the installer adds a scoped `NOPASSWD` rule for exactly
`wg-quick up/down <iface>`. If a site blocks all outbound UDP, a TCP reverse-SSH fallback is in
[BASTION.md](BASTION.md).

## 8. OTA updates

`update_agent` / `update_system` fetch a release, verify its **sha256 + Ed25519 signature** against
`update_public_key`, hand the privileged install to a root helper, atomically swap `current →
releases/{version}`, **health-check**, and **roll back** automatically on failure. The agent never
holds root itself. Releases are produced with `tools/sign_release.py`.

## 9. Module map

| File | Responsibility |
|---|---|
| `main.py` | cycle loop, command effects, telemetry/status/ack builders |
| `config.py` | load/save the YAML config (+ persisted runtime state) |
| `readers/base.py`, `readers/solarlog_getjp.py` | device-specific reading (thin relay) |
| `transport.py` | MQTT/TLS connect / publish / retained-command fetch |
| `buffer.py` | SQLite store-and-forward |
| `commands.py` | dispatch remote commands to agent effects |
| `enroll.py` | datalogger auto-discovery + `local_ips()` |
| `remote.py` | WireGuard up/down (or no-op when externally managed) |
| `update.py` | signed OTA + rollback + OS version |

Everything is unit-testable without a Pi, a datalogger, or a broker (`transport._run`,
`remote._run`, the reader's HTTP, and the clock are the only seams). See README → *Development*.
