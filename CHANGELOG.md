# Changelog

All notable changes to the Experanto Edge agent are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/).
A `!` marks a **breaking change** (behaviour or config default changed).

## [Unreleased]

## [0.5.0] — 2026-08-03

_Second brand on the edge: the Azzurro/ZCS Hub, read passively over its local WebSocket._

### Added
- **`zcs_hub_ws` reader** (`reader_type: "zcs_hub_ws"`) — talks to the Azzurro/ZCS
  Hub on its local, unauthenticated WebSocket (fixed port **55558**): sends
  `{"head":"stsreq"}` and forwards the `body` of the `status` reply **verbatim**
  as `{"read_at": <float>, "zcs": {"status": ...}}`. Thin relay as ever: no value
  is parsed on the Pi. The WS was chosen over the documented Modbus TCP 55400
  because the hub's Modbus accepts **one connection per port** (a reader would
  lock out the customer's own cloud), while the WS is multi-client and exposes
  ~90 quantities per inverter including **native MPPTs**.
  **Read-only:** the same socket is also the hub's control channel — the reader
  never sends `scan`/`cmd`/`kill_app`/`cfg_refresh`.
- **Zero-dependency WebSocket client** (RFC 6455, ~100 lines on `socket`+`struct`):
  masked client frames, 7/16/64-bit lengths, continuation reassembly, ping→pong,
  one overall deadline (default 20s, live the status lands in ~0.1s), socket
  always closed. Any network/protocol/JSON failure surfaces as `ReaderError` —
  the only exception the agent catches.
- **`discover()` on the ZCS hub** returns the inverter roster only
  (`serial`/`index`/`status`/`modbus_addr` from `STS__INVERTER_SCAN`, empty slots
  skipped, empty field → `None` never `0`); the 156 KB status is not re-forwarded
  there, telemetry already carries it.

### Notes
- `fetch_history_curves` is **not** implemented for `zcs_hub_ws`: the contract
  default (`None`) stands, so an on-demand history command gets an honest
  "not supported" ack instead of a fabricated curve.
- No LAN auto-discovery for this reader: `datalogger_ip` is required (absent →
  `ReaderError`). The inline discovery in `main.py` stays solarlog-only.

## [0.4.3] — 2026-08-02

_On-demand history hardened for big/slow dataloggers (Curinga: 97 inverters)._

### Added
- **`history_spacing` config field** (float seconds, default `0.0` = burst,
  unchanged behaviour) — pause between the per-device 143 history queries.
  Old/slow dataloggers with many inverters 503 under the burst; raise it
  (e.g. `1.0`) on those. In the `set_config` whitelist (range `[0, 30]`),
  hot-applied to the reader, no restart needed.
- **Per-device retry** in `fetch_history_curves` (2 attempts, pause
  `max(history_spacing, 2s)`) — but ONLY on transport errors (503/timeout/
  truncated JSON). A valid response without the requested node is DATA (the
  datalogger has no archive for that daysback): no blind retry, and the
  device is INCLUDED with a `None` node so the server sees
  "present-but-empty" and closes the day instead of retrying it forever.
  On password-protected dataloggers the session is re-established inside
  the attempt loop (a 503 clears it; without re-login every subsequent
  query was silently unauthorized). One retry on the 860 probe too (it
  gates the mapping of every curve).

### Fixed
- **Honest `fetch_history` ack** — `total_devices` is now the number of
  devices that SHOULD have been read (reader-reported `expected`), not just
  the successes: a collection missing a device reaches the server as
  `done=false` instead of being passed off as complete. `expected == 0`
  (datalogger unreachable, no known devices) acks `ok=false` — never
  "0 of 0 = success". Backward compatible with pre-0.4.3 readers (fallback
  to `len(curves)`).

## [0.4.2] — 2026-07-30

_Host self-report: each instance declares which Pi it runs on (cross-check, never authority)._

### Added
- **`host_device_code` config field + status self-report** — the `up/status`
  envelope now carries `host_device_code`: the `device_code` of the Pi HOST's
  `edge_devices` row this instance runs on (equal to `device_code` on
  self-hosts, where the datalogger row IS the Pi). Purely additive (0.4.1
  consumers ignore it). Server-side it is persisted as `reported_host_code`
  and cross-checked against the operator-set authoritative link
  (`edge_devices.host_device_id`, mig 37): a divergent report fails the
  web-shell mono-tenant policy closed. The report is NEVER an authority —
  a device credential cannot re-parent itself.
- **`--enroll CODE:SECRET[:STATION_ID[:HOST_CODE]]`** — optional 4th part
  writes `host_device_code` at enrollment; `install.sh --host-code EXP-XXXX`
  passes it through (empty station stays empty: `CODE:SECRET::HOST` works).

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
