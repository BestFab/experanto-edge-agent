# Changelog

All notable changes to the Experanto Edge agent are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/).
A `!` marks a **breaking change** (behaviour or config default changed).

## [Unreleased]

## [0.4.1] — 2026-07-28

_Remote repair: il sandbox resta intatto, ma OTA/reboot tornano possibili e il tunnel WG si auto-ripara._

### Added
- **Helper broker (path-unit root)** — the fleet's systemd sandbox
  (`NoNewPrivileges=true`) blocks `sudo -n ota-helper.sh`, which made `reboot`,
  `update_agent` and `update_system` dead on arrival (proven live on the fleet:
  ack `ok=false "no new privileges flag is set"`). The agent now falls back to a
  file-brokered channel: it writes `{state}/helper.request`
  (`<epoch> <nonce> <action> [args]`), the root `experanto-edge-helper.path`
  unit triggers `helper-broker.sh`, which validates the request and launches
  `ota-helper.sh` in a **transient `systemd-run` unit** (own cgroup, no inherited
  timeout, survives an agent restart), answering on `{state}/helper.response`.
  Channel order in `_launch_helper`: `sudo` first (instantaneous where it works),
  broker on failure; both errors surfaced together in the ack. The agent sandbox
  is UNCHANGED. Hardening from adversarial review:
  - The response (and its temp) is written in a **root-owned dir**
    (`/run/experanto-edge`, the unit's `RuntimeDirectory`), never in the
    agent-owned state dir — so root never writes by-path where the sandboxed
    user could unlink+symlink it (the old `mktemp`+`> "$tmp"` in the state dir
    was still TOCTOU-swappable, and `chown` is gone entirely). The agent only
    **reads** the response there.
  - A `helper.request` that is itself a **symlink** (e.g. → `/etc/shadow`) is
    detected after consumption and dropped unread, so root never leaks a
    sensitive file's first token into the response.
  - `version` (which reaches `rm -rf "$RELEASES/$version"` in the helper) is
    charset-guarded `^[A-Za-z0-9._-]+$`; `artifact` is confined to the state dir
    via `realpath` prefix (the `case` glob alone let `..` escape). Closed.
  - `nonce` is validated (goes into a unit name); the broker no longer `exec`s
    the helper inside its own oneshot job, so `TimeoutStartSec` can't kill a long
    `apt`/`pip` mid-transaction and the path-unit re-triggers immediately.
  - `_helper_dir` is a fixed path (default `/var/lib/experanto-edge`, matching the
    path-unit) rather than derived from `buffer_path`, so a moved buffer can't
    orphan the channel.
- **WireGuard watchdog (`wg-watchdog.sh` + timer)** — root-level self-heal,
  fully outside the agent: every 10 min, if `wg-quick@<iface>` is enabled and
  the newest peer handshake is older than 35 min (or the interface is gone),
  bring the tunnel back, with a 30-min cooldown against flapping. `--check`
  prints the decision without acting. Would have auto-repaired the Curinga
  outage of 2026-07-26 (tunnel died in place, host up, MQTT alive). Review
  hardening: recovery is explicit `stop`+`wg-quick down`+`ip link del`+`start`
  (a bare `restart` on an inactive unit skips `ExecStop` and dies with "already
  exists" when the iface was brought up out-of-unit); `hs==0` is only stale once
  the interface has been up longer than the threshold (no boot-race churn while
  the peer isn't yet registered on the hub).
- `install.sh` installs and enables both: `experanto-edge-helper.path` +
  `.service`, `wg-watchdog.timer` + `.service`, `helper-broker.sh`,
  `wg-watchdog.sh`.

## [0.4.0] — 2026-07-22

_Fase G «Allineamento worker»: comandabilità remota senza OTA + affidabilità dei publish._

### Added
- **`set_config` remote command** — changes a **whitelisted** subset of the config remotely:
  `collect_inverter_detail`, `persistent_commands`, `interval`, `command_wait`, `log_level`.
  Atomic type/range validation (one bad key rejects the whole command), persisted to the local
  YAML (rolled back in RAM if the save fails), ack `detail` =
  `{"applied": {...}, "restart_required": [...]}`. `collect_inverter_detail` and `log_level`
  are applied live; `persistent_commands` takes effect after a `restart` command. **No
  network/WireGuard/broker/identity/OTA/path key is remotely settable** (tripwire test). With
  OTA gated by the systemd sandbox, this unblocks per-inverter detail and on-demand history
  on the live fleet. See HOW_IT_WORKS §4 for the exact contract.
- **`fetch_history_curves` is now part of the `Reader` contract** (`readers/base.py`), with a
  default of `None` = "on-demand history not supported". The agent distinguishes that honest
  declaration from an empty result: a conforming reader without an override acks
  `ok=false "senza storico on-demand"` instead of a fake 0-device success.
- **Critical publish retry** — acks and `up/history` chunks (which, unlike telemetry, have no
  store-and-forward buffer) are published via a retry helper (3 attempts, `TransportError`
  absorbed). `fetch_history` now reports its real outcome: lost chunks after retries make the
  ack `ok=false` with `detail.done=false` (+ `sent`/`n_devices`), so the server can tell a
  partial curve from a complete one.

### Changed
- **Datalogger autodiscovery is concurrent and time-bounded** — probes run in waves of 32
  threads (an empty /24 takes ~3s) under a 20s budget, instead of a sequential 254×0.4s sweep
  (up to ~100s blocking startup; with `Restart=always`+`RestartSec=10` a powered-off datalogger
  caused scan/restart cycles). Deterministic like the old scan: the lowest responding host in
  subnet order wins (matters with two dataloggers on one LAN, the real `.57`/`.59` case).
- `Transport.connect()` (abstract) now declares the `persistent` flag its real implementation
  already had, so fakes and future transports match the contract.

### Fixed
- **`read_now` was a no-op in the default (intermittent) mode** — the command's wake-up was
  cleared right *after* `run_cycle()` had handled it, so the Pi acked "lettura immediata
  programmata" and then slept the full interval anyway. The wake is now cleared *before* the
  cycle; `read_now`/`rediscover` really do trigger an immediate next cycle. (The persistent
  loop was already correct.)
- The `fetch_history` ack `detail` JSON is serialized with sorted keys (deterministic).

## [0.3.3] — 2026-07-18

_Entry ricostruita: la 0.3.3 era stata rilasciata senza voce di changelog._

### Added
- **On-demand history curves** (`fetch_history` command) — the reader forwards the raw
  per-inverter day curve (`143:100` + `860`) and the agent publishes one `up/history` chunk
  per device, correlated by `command_id` (`experanto.edge.history/1`).
- **Opt-in persistent MQTT connection** (`persistent_commands`, default **false**) — commands
  are delivered instantly (needed by on-demand history: the server waits ~22s); telemetry
  stays on the `interval` timer. The default intermittent path is unchanged.

### Fixed
- A command handler that raises (e.g. `cfg.save` `PermissionError`) no longer stops the
  persistent loop.

## [0.3.2] — 2026-07-16

_Fix: 143 device index is the FIRST sub-key, not the last._

### Fixed
- **Per-inverter detail was reading one fixed device for all inverters.** The `143` device
  index is the **first** sub-key (`{"143": {"<dev>": {"101": {"0": null}}}}`), not the last
  (`{"143": {"1": {"101": {"<dev>": null}}}}`). Verified live: varying the last key returns the
  same device; varying the first key selects it. Now each inverter gets its own temp/MPPT/phases.

## [0.3.1] — 2026-07-16

_Per-inverter detail rework: usable telemetry via 860 + 143 current-values._

### Changed
- **Per-inverter detail now forwards `860` + `143:101` (current values)** instead of `143:100`
  (full-day curve) + `870`. The `860` "channel wall" turned out to be a request-format issue —
  the bare `{"860": null}` makes the datalogger 500, but the **indexed** `{"860": {"<idx>": null}}`
  returns 200 (503s were just rate-limiting). The reader now pages the `860` epochs, keeps the
  current one (highest index = live config layout), and forwards its `channels.min` (per-device
  `[type, channel]` map) so the server can label the `143` columns. `143:101` is the **current
  values** row (~208 B/inverter vs ~88 KB for the full-day curve), so the payload stays tiny.
- `860` is static, so it is fetched on the history cadence and **cached**, but included in every
  snapshot (the server is stateless and needs it to map each cycle).

### Notes
- The column→field mapping lives entirely server-side (`impianti/solarlog_local.py`); the reader
  stays a thin relay. Validated end-to-end against a real Growatt MAX-125KTL3-XLV plant.
- `collect_inverter_detail` remains **off by default**; enabling it adds one `143` request per real
  inverter per cycle (plus the hourly `860` refresh), so roll out per-datalogger and watch for 503s.

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
