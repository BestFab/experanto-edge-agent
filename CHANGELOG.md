# Changelog

All notable changes to the Experanto Edge agent are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/).
A `!` marks a **breaking change** (behaviour or config default changed).

## [Unreleased]

## [0.3.0] — 2026-07-16

_Solar-Log data enrichment + WireGuard safety._

### Added
- **Per-inverter status** — the reader forwards getjp `608` (per-device `Normal`/`OFFLINE`/`RUNNING`;
  `RUNNING` marks a meter, not an inverter).
- **Real serials + history** — the reader forwards getjp `740` (per-inverter serial numbers) and,
  throttled, `877`/`878` (monthly/yearly production history) so the server can show past periods.
- **Per-inverter detail** — optional getjp `143` (temperature, per-string Udc/Idc/Pdc, per-phase
  Uac, frequency), gated by the new config `collect_inverter_detail` (**off by default**: the full
  intraday series is heavy and its column map needs the datalogger's `860`/`870` blocks). When on,
  it queries only the real inverters (from `740`), never every device slot.
- **Anti-503 spacing** — configurable delay between getjp queries; the Solar-Log Base returns 503
  when polled too fast.

### Changed
- **!** **WireGuard is externally managed by default** (`wg_managed_externally=True`). When the
  overlay is a persistent system service (`wg-quick@<iface>` enabled at boot — the Pi's lifeline),
  the agent no longer runs `wg-quick up/down` on it: `open_ssh`/`close_ssh` become no-ops on the
  interface, so no agent action can ever tear down connectivity. Set it `false` only on a Pi where
  the agent owns an on-demand interface (no persistent overlay) — the historical behaviour.

### Fixed
- The `143` reader forwards the last row that **has data**, not the literal last row — at night the
  recent intraday slots are all `None` (inverter asleep).
- **OTA no longer reports false success** — `_launch_helper` now watches the detached root helper
  briefly and surfaces an immediate `sudo -n` escalation denial (a `NoNewPrivileges` sandbox or a
  missing sudoers rule) instead of returning "started" while nothing ran.
- **Installer** — `/etc/experanto-edge` is created owned by the service user (was `root:root`), so
  the agent can persist its config atomically instead of failing every save with `PermissionError`.

## [0.2.0] — 2026-07-12

### Added
- **Self-bootstrapping installer** — `install.sh` downloads the latest signed release and sets up a
  hardened systemd service (start on boot, auto-restart). One-liner bring-up.
- **`tools/recon.py`** — stdlib-only tool to sweep the LAN and fingerprint an unknown datalogger
  (Solar-Log getjp, Fronius Solar API, SMA, Modbus TCP/SunSpec, SNMP) and suggest a reader.
- Documentation: full `README.md` + `SETUP.md` (production runbook), `WG_HUB.md`, `BASTION.md`.

### Changed
- **!** **Remote access is now self-hosted WireGuard** to your own hub — no third-party service.
  This replaced the reverse-SSH tunnel, which had replaced the initial Tailscale integration.

### Fixed
- A missing or unreachable datalogger no longer crashes the agent at boot: it starts, reports the
  error in its status, and retries on the next cycle (no crash-loop).

## [0.1.0] — 2026-07-11

_Initial agent — the E0/E5 skeleton._

### Added
- **Agent core** — the intermittent cycle: read the local datalogger, connect to the broker over
  MQTT/TLS, flush any buffered readings, publish telemetry + status, pick up one pending command,
  disconnect. Offline is detected server-side from stale telemetry.
- **Store-and-forward buffer** — unsendable readings are persisted to SQLite and flushed on the
  next successful connect.
- **Remote commands** — `read_now`, `set_interval`, `rediscover`, `get_diag`, `restart`, `reboot`,
  `update_agent`, `update_system`, `open_ssh`, `close_ssh`.
- **Signed OTA (Ed25519)** for both the agent and the OS, with sha256 verification, atomic swap,
  health-check, and automatic rollback.
- **Enrollment / auto-discovery** of the Solar-Log on the LAN.

### Fixed
- Socket errors normalized to `TransportError` (so store-and-forward engages cleanly).
- `local_ips()` reports the real LAN IP (for SSH reach hints and diagnostics).
