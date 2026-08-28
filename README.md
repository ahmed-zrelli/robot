# Outdoor Robot Monitor — V0

Monitoring-only dashboard. Two telemetry sources behind one wire format
(`{type:'msg', topic, t, data}`), so the frontend never knows which one is
active:

- **bag mode** (default) — replays `backend/data/*.json`, extracted ahead
  of time from a recorded rosbag by `extract_bag.py`.
- **live mode** — subscribes to a real ROS1 system over rosbridge (the
  virtual robot container today, the real robot later). Topics and types
  are discovered from the robot itself at connect time (`rosapi`) — no
  dependency on `backend/data/` at all.

## Run it

`backend/data/*.json` is included, so bag mode runs out of the box:

```
cd backend
pip install -r requirements.txt
python -m uvicorn main:app --port 8123
```

Open `http://localhost:8123`, pick a playback speed, click **Connect**.

## Virtual robot mode (Docker)

Point the dashboard at a Docker container that plays the bag as live ROS1
topics over rosbridge, so the backend subscribes the same way it eventually
will to the real robot — zero dependency on `backend/data/` in this mode.

1. Put your `.bag` file in `bag/`.
2. `docker compose up --build` — builds the ROS1 Noetic image
   (`roboteq_msgs`/`enova_msgs` compiled in) and starts everything:
   `sshd`, the speed-test target, and the ROS stack (`roscore`, `rosbridge`
   on `ws://localhost:9090`, the relay, and looped `rosbag play`) all come
   up automatically — no manual SSH step needed. Give it a few seconds
   after the container starts before connecting; tail
   `docker compose logs -f` or `/var/log/ros_stack.log` inside the
   container if you want to watch it come up.
3. Run the backend with `TELEMETRY_SOURCE=live` set:
   ```
   cd backend
   pip install -r requirements.txt
   $env:TELEMETRY_SOURCE="live"     # PowerShell
   python -m uvicorn main:app --port 8123
   ```
4. Open the dashboard, click **Connect**.

No `TELEMETRY_SOURCE` set = bag mode.

(SSH is still available if you want to poke around manually — see below —
it's just no longer required to get data flowing. Whether the real robot's
ROS stack auto-starts or needs a manual launch is a separate, unconfirmed
question that doesn't affect this dashboard either way — see "Connecting
to the real robot".)

### Poking around inside the virtual robot manually

Port 22 isn't published to the host (nothing requires SSH anymore — see
above), so use `docker compose exec` instead of `ssh`:
```
docker compose exec virtual-robot bash
rostopic list
rostopic echo /battery/state
rosmsg show roboteq_msgs/Feedback
```
Same discovery workflow the dashboard's live mode uses programmatically
(`rosapi/topics`), just manual — useful for poking around against the real
robot later too (there it'll actually be `ssh`, since there's no `docker
compose exec` on a real robot).

### What's still manual, and why

- **Panel routing** (`PANEL_MAP` in `main.py`) — a topic name alone
  doesn't say "this is the battery panel." Any topic not in `PANEL_MAP`
  still streams over the WebSocket in both modes, it just has no panel to
  render into yet.
- **Live-mode message packages** — the container only has `roboteq_msgs`
  and `enova_msgs` compiled in. Anything else gets picked up automatically
  by `relay_node.py` — no compiled package needed, works for any topic:
  it relays every topic through `rospy.AnyMsg`, reads the publisher's live
  field layout off the TCPROS connection header, and builds a real message
  class at runtime via `genpy.dynamic.generate_dynamic(...)`.
  `ros_live_source.py` discovers and re-presents `/relay/*` topics under
  their original names, so `main.py`/the frontend never know a relay was
  involved. Fully built and wired, not pending.

## Connecting to the real robot (2026-08 note)

The Docker "virtual robot" above (SSH in, run `start_robot`) is a **local
dev/test simulation only** — it exists so `live` mode can be built and
tested without a real robot available. It was never meant to describe the
real connection procedure, and confirmed guidance is that the real robot
will NOT be reached this way. Nothing in `backend/` changes because of
this — `ROSBRIDGE_HOST`/`ROSBRIDGE_PORT` (see `main.py`) were already
env-var-driven for exactly this reason: `live` mode has zero dependency on
`localhost`, Docker, or SSH — pointing it at any reachable rosbridge
server, anywhere, was always the design.

**Real-world plan (per supervisor guidance):** connect to the robot
on-site via a wired Ethernet cable, into either the robot's LTE router
(Semtech AirLink RV55, formerly Sierra Wireless — most likely target,
since onboard compute usually has its single Ethernet port already
occupied by the router, whereas routers are built with spare LAN ports for
exactly this kind of technician access) or directly into the onboard
computer's NIC if a port is free there instead. Confirmed: the robot runs
**ROS1** (distro/version not yet confirmed).

This resolves two of the six original open unknowns (see "Real robot -
open questions" below): the network path is wired/on-site, not the RV55's
LTE/cellular link, and whatever "connect securely" means is likely
answered by physical access being the security boundary — though that's
worth confirming rather than assuming.

**Still unconfirmed, and blocking before `live` mode can point at the real
robot:** whether `rosbridge_server` is even running there.
`rosbridge_server` isn't a default part of any ROS1 install — it's a
separate package that must be explicitly installed and launched (this
project's `start_ros_stack.sh` does exactly that for the virtual robot).
Enova's robot almost certainly runs ROS for its actual job (motor control,
sensors, nav); that doesn't imply anyone added rosbridge on top of it
specifically for this dashboard.

To check, once there's a network path in:
```bash
# with shell access to the onboard computer:
rospack find rosbridge_server        # is it installed?
rosnode list | grep rosbridge        # is it currently running?
ss -tlnp | grep 9090                 # is anything listening on 9090?
```
```powershell
# from the laptop, without needing shell access at all:
Test-NetConnection -ComputerName <robot-ip> -Port 9090
```
If that succeeds, try pointing the dashboard at it directly
(`ROSBRIDGE_HOST=<robot-ip>`) and see if `live` mode actually connects —
that's the real test, more reliable than inferring from the port check
alone.

## Real robot — open questions (not yet resolved)

Tracked here rather than left implicit, so nothing gets silently assumed:

1. Exact ROS1 distro/version (Noetic assumed in the virtual robot; not
   confirmed on the real robot).
2. Current motor controller package/driver in use.
3. LiDAR topic name and message type.
4. Whether `rosbridge_server` is installed and running on the real robot
   (see above) — and if not, how it would get installed/launched, and by
   whom.
5. ~~Network connectivity path~~ — **resolved for on-site work**: wired
   Ethernet, likely into the RV55 router. Remote/production monitoring
   path (if any, possibly over the RV55's LTE link) is still open and is
   probably a separate question from on-site debugging.
6. What "connect securely" means concretely, still worth confirming
   explicitly rather than assuming "physical Ethernet access is enough."


## Re-extracting from a raw bag (optional)

The raw `.bag` isn't included (too large). To regenerate
`backend/data/*.json`:

1. Drop a `.bag` file into `bag/` (any single `.bag` file there is used).
2. `python backend/extract_bag.py` — extracts every topic actually present
   (no whitelist), overwrites `backend/data/*.json`, drops stale JSON from
   a previous bag's topics that no longer exist.

High-rate topics are decimated at extraction time to a flat rate
(`DEFAULT_DISPLAY_HZ = 5` in `extract_bag.py`) — for browser readability
and payload size, not a claim about the sensor's real publish rate. This
happens **on the data itself**, not just at render time: bag mode's
`HealthMonitor` (and everything else) sees the decimated 5Hz stream, same
as the frontend. In practice this doesn't matter for the current checks
(180s-window z-scores are unaffected by going from e.g. 50Hz to 5Hz), but
if a future check needs true native-rate data, extract a non-decimated
copy rather than assuming "display only" — that framing was inaccurate.

## What's real vs. placeholder

| Panel | Status |
|---|---|
| Battery | Live — `/battery/state`, `/charging` |
| Motors (4x) + driver status/faults | Live — `roboteq_msgs` feedback/status |
| GPS/GNSS | Live — `enova_msgs` GnssFix/GnssStatus |
| Network / 4G | **Placeholder** — no topic in the bag, RV55 config unconfirmed |
| LiDAR | **Placeholder** — no topic in the bag |
| Speed test | Real — backend-proxied (browser → backend → speed-test target), matching how telemetry already works; see `virtual_robot/speedtest_server.py` and `backend/main.py`'s `/api/speedtest/*` routes. Ping/download/upload all verified end-to-end against the target running standalone. `docker compose up --build` itself has not been run — this was tested by running `speedtest_server.py` and `main.py` directly, not inside Docker. **Gated** on an active live robot connection — see "Network Speed Test vs. Telemetry Throughput" below. |
| Telemetry throughput | Real — live-mode only, computed from actual rosbridge message arrivals (same source as per-topic Hz), not a synthetic probe. See below. |
| Secure connect/disconnect | Connect/Disconnect open/close the WebSocket and clear telemetry on disconnect. Not yet the "secure" transport to the real robot — depends on how the robot exposes data (rosbridge/MQTT/VPN/etc.), still unconfirmed |
| System Health | Live — see below |

## Network Speed Test vs. Telemetry Throughput

Two different metrics that can look similar but answer different
questions. Don't conflate them — a passing one does not imply the other
is fine.

| | **Network Speed Test** (`/api/speedtest/*`) | **Telemetry Throughput** (`meta_update.telemetry`) |
|---|---|---|
| What it measures | Raw network-layer capacity to the robot's speed-test target | Actual ROS message data rate over rosbridge |
| Data | Synthetic — 20MB download / 10MB upload against `virtual_robot/speedtest_server.py`, a dedicated HTTP endpoint on its own port (`8091`), unrelated to ROS | Real — bytes/messages from actual topic traffic the dashboard is already receiving |
| When it runs | On demand (button click) | Continuously, live, while connected |
| Requires | A live robot connection (gated - see below) | A live robot connection actually delivering messages |
| Answers | "Is the physical/network path healthy?" — can work even if rosbridge or ROS itself isn't running yet, since it doesn't touch either | "Is data actually flowing right now, and how much?" |
| Can't tell you | Whether ROS/rosbridge is up, or whether any topic is actually publishing | The network's total capacity — only current utilization. A robot publishing very little data can show tiny throughput on a link that's actually capable of much more |

They're complementary, not redundant: a good speed test result with zero
telemetry throughput means "network's fine, but rosbridge/ROS isn't
delivering anything" — a real, distinct failure mode from "the network
itself is the problem." Both being explicit avoids conflating them
into one ambiguous number.

Telemetry throughput is computed inside `_stream_live()` from the exact
same `arrival_times` tracking already used for per-topic Hz — no new
sampling mechanism, no synthetic traffic, just byte-counting the
sanitized JSON payloads (topic/wrapper fields excluded) that were going
to be sent to the frontend anyway. It's live-mode only: bag mode replays
recorded data, which isn't "the robot's current link" in any meaningful
sense, so it deliberately reports nothing there (frontend shows `--`).

**Both currently gated behind an active live connection** (see
`_robot_connected()` / `_live_connections` in `main.py`) — speed test
explicitly rejects calls when disconnected; telemetry throughput simply
has nothing to report since it's computed inline as messages arrive.

## System Health

A banner (OK / WARNING / CRITICAL) plus a live event feed, implemented in
`backend/health_monitor.py`. Built with **no labeled fault dataset**, which
shapes what it can honestly claim to do — it detects and classifies
*current or recently-sustained* state, not causes, and doesn't predict
failure before it happens (see "Not yet implemented" below).

### Four kinds of thing being tracked

1. **Anomaly** — statistically unusual, unclassified, may be nothing.
2. **Fault** — a discrete, named state a component reports itself (motor
   controller's own bitmask: OVERHEAT, OVERVOLTAGE, UNDERVOLTAGE,
   SHORT_CIRCUIT, EMERGENCY_STOP, etc.). Real classification, for free —
   the code just surfaces what the controller already knows.
3. **Degradation** — performance measurably worse, robot still functions
   (currently: GNSS fix below 3D).
4. **Failure** — a fault or degradation that persists past a
   capability-specific recovery window and blocks a **core capability**
   (mobility / localization / power). A second event, additive on top of
   the underlying one — the original keeps firing/clearing independently.

### The checks

- **Hardware faults** (`hardware_fault`, CRITICAL) — direct from the
  controller bitmask. Edge-triggered, no debounce; it's already a
  confirmed discrete report, delaying it would only hurt.
- **Statistical anomalies** (`statistical`, WARNING) — rolling per-field
  baseline (mean/std) over the last `WINDOW_SECONDS` (180s) of **wall-clock**
  time (see "Known limitations" below re: bag replay speed), flagged when
  z-score passes `Z_THRESHOLD` (3.5) for `CONSECUTIVE_REQUIRED` (3) samples
  in a row. Separate idle/driving baselines per field (gated on whether any
  motor's last `commanded_velocity` is nonzero) so a single mixed baseline
  doesn't misjudge either state. Monitored fields: `battery.voltage/
  current/temperature`, and each motor's `motor_current/
  channel_temperature/supply_voltage`.
- **Commanded-vs-measured velocity residual** (`residual`, WARNING) — per
  motor, a persistently large gap between commanded and achieved velocity
  (slip/stall/load signal). Same windowing/debounce as above.
- **GNSS fix degradation** (`gnss`, WARNING) — fix drops below 3D.
  Edge-triggered, no debounce, same reasoning as hardware faults.
- **Sensor liveness** (`sensor_health`, its own category — a monitoring
  gap, not a robot state) — a field bit-identical across the entire
  rolling window (`DEAD_SENSOR_MIN_SPAN_SECONDS` = 90s,
  `MIN_BASELINE_SAMPLES` = 25+) is flagged dead/suspect.
  `channel_temperature`/`motor_current` are judged only against the
  driving-state pool (`DRIVING_GATED_LIVENESS_FIELDS`) since they're
  physically expected to go flat while idle. Never escalated to
  `failure` — going dead means this code can't tell what state the robot
  is in, not that the robot is broken.
- **Field coverage** (`HealthMonitor.field_coverage_warnings()`) — a
  different problem from liveness: a field that *never arrives at all*
  (wrong field name for this message version — e.g. this bag's older
  `sensor_msgs/BatteryState` predates the `temperature` field it's
  configured to watch) never even reaches the liveness check, so it was
  silently invisible everywhere. This flags any `MONITORED_FIELDS` entry
  that's never once arrived as a valid number despite its topic
  publishing 100+ messages. Logged to the **server console only**
  (`main.py`, deduped on `(topic, field)` per session) — deliberately not
  a dashboard event, since a schema mismatch is a code/config problem
  found once at session start, not a robot state that changes over time.
- **Comms / topic liveness** (`comms`) — tracks each topic's own recent
  median inter-arrival interval (self-calibrating, no hardcoded expected
  rate) and flags a topic stale once overdue by
  `max(STALE_FLOOR_SECONDS=5.0, STALE_MULTIPLIER=5.0 × its own median)`.
  Only evaluated when *some* message arrives (no background timer), so
  total silence across every topic at once isn't caught until the next
  message anywhere arrives. Never escalated to `failure`, same reasoning
  as sensor liveness.
- **Failure escalation** — if `hardware_fault`, `residual`, `gnss`, or
  `statistical` stays continuously active past its window
  (`ESCALATION_SECONDS`: hardware_fault 0s, residual 15s, statistical 20s,
  gnss 30s), a `failure` event fires, tagged with the impaired capability.
  `sensor_health` and `comms` are explicitly excluded.

### Reference numbers (full 212-min bag replay, current code)

| Kind | Total | New (first-fire) |
|---|---|---|
| statistical | 488 | 244 |
| residual | 52 | 26 |
| hardware_fault | 20 | 10 |
| gnss | 33 | 17 |
| sensor_health | 2 | 1 |
| comms | 68 | 36 |
| failure | 51 | 26 |

Plus exactly one `field_coverage_warnings()` hit:
`/battery/state: 'temperature' has never arrived as a valid number in
12758 messages on this topic`. Reproduce by loading `data/manifest.json`,
merging all monitored-topic rows by `_t`, and feeding through
`HealthMonitor().ingest()` — do this for any future change rather than
trusting a written description of its effect, including this one.

## Not yet implemented — needs data or your input, not guessed at

- **Charging subsystem** (`/charging`, `/coil_outputs`) — the reference
  bag never shows a charging event (`/charging` is `False` and
  `/coil_outputs` is `[0,0,0,0]` for the entire 212-minute run), so there's
  nothing to learn the semantics from. Needs either a bag that actually
  contains a charge cycle, or your domain knowledge of what a normal vs.
  failed pattern looks like in these fields.
- **GNSS accuracy degradation beyond `fixType<3`** — `carrierStatus`/
  `diffMode`/`diffSource` meanings are confirmed (from
  `enova_msgs/GnssStatus.msg`'s own field comments). `/ublox/rtk_correction`
  does vary in the reference bag and correlates with fix quality, but the
  scenario a new check would exist to catch — corrections unavailable
  while the fix otherwise stays good — occurs in only 1 of 37,371 samples.
  Not enough to derive a duration/debounce threshold without tuning to
  noise. On hold until a bag/session shows real independent RTK variation.
- **Multivariate/joint anomaly detection** (Mahalanobis / PCA
  reconstruction error) — the honest next tier beyond per-field z-scores,
  for a correlated anomaly that's subtle per-field but obvious jointly.
  Not built — no evidence per-field checks are currently insufficient, and
  no labeled data to validate against.
- **Failure anticipation/prediction** — the project's actual original
  goal. Everything built so far detects/classifies *current or
  recently-sustained* state; none of it predicts a failure before it
  happens. Needs historical labeled failure examples (none exist) or a
  deliberately-designed unsupervised precursor approach, worked out with
  you from first principles rather than built speculatively. **Likely the
  next major focus.**
- **`supply_voltage` remaining noise** — still the largest single
  contributor to statistical events (154/244) even after the idle/driving
  baseline split. Likely genuine continuous variation while actively
  driving, not an artifact — the split fixed cross-contamination between
  states, this is what's left over. Not flagged as broken.

## Known limitations (documented, not fixed — deliberate or low-priority)

- **Statistical windows don't compress with bag playback speed** —
  `WINDOW_SECONDS`/`MANEUVER_SETTLE_SECONDS`/etc. are wall-clock by
  design (`health.ingest()` always gets `time.time()`, so `warm_up()` and
  live ingestion share one clock — see `db.recent_telemetry()`). Side
  effect: at the default 20× replay speed, the "180s" statistical window
  actually spans ~3600s (1 hour) of bag-time, not the 3 minutes the checks
  are tuned around. The backend now logs this once per non-1× bag session
  (see `main.py::_stream_bag`) so it's visible rather than silent. Whether
  the window should track bag-time instead at non-1× speed is a design
  call for a future session, not a bug fix — flagging here in the
  meantime. **Not just theoretical** — `scratch/sweep.py`/
  `scratch/replay_harness.py` now take a real `speed` argument that
  mirrors `_stream_bag`'s own wall-clock-vs-bag-time pacing (see their
  docstrings), and reproducing the shipped config (`DEBOUNCE_SECONDS=2.0`,
  `STALE_PERCENTILE=99.0`, `STALE_SCOPE=all`) at 1×/5×/20× on the
  reference bag gives 136/66/46 total new events respectively (`scratch/
  sweep_results.csv`) — the whole detection profile shifts with speed, not
  just a linear delay, because the widened window changes what counts as
  anomalous in the first place. Re-run `python scratch/sweep.py 2.0 99.0
  all <speed>` if `DEBOUNCE_SECONDS`/`WINDOW_SECONDS`/the default `speed`
  in `main.py` ever change, since these numbers will go stale.
- **`capability`/`started_at` aren't persisted to the DB** — every
  `HealthMonitor` event carries these (see `_emit_new()` in
  `health_monitor.py`), but `db.log_event()` only stores `key, kind,
  severity, signal, message, state`. Fine for V0 (nothing today reads
  history filtered by capability), but would need adding if
  capability-filtered history or duration analysis on past sessions is
  ever wanted.
- **Synchronous commit-per-row in `db.py`** — every `record_telemetry`/
  `log_event` call does its own `conn.commit()`. Given the README's own
  "single file, stdlib `sqlite3`, simple V0" scope and current message
  rates, this is a deliberate simplicity choice, not an oversight — won't
  matter until throughput or concurrent sessions grow well past this
  project's current scale.

## Fixed this session

- **NaN breaking the frontend on real GPS data** — `/ublox/gnss_fix` has
  138/49,768 rows with `lat`/`lon` = NaN (mostly `fixType=0`, before a fix
  is computed). Python emits the bare `NaN` token, which is valid Python
  but not valid JSON; JS's `JSON.parse` threw on it, silently dropping
  those messages before the frontend's own `isFinite()` guards ever got a
  chance to run. Fixed at the source: `main.py::_sanitize()` recursively
  converts NaN/±Infinity to `null` before anything is sent over the
  WebSocket or written to the DB.
- **`list_sessions` missing telemetry-only sessions** — it grouped by
  `health_events` only, so a session that streamed telemetry but never
  triggered a health event was invisible from `/api/sessions` (though
  still fetchable directly by ID). `latest_session_id()` already fell back
  to `telemetry` for this same case; `list_sessions()` now unions both
  tables the same way.
- **Leftover debug logging** — removed a `print("GOT MESSAGE:", topic)`
  firing on every native-subscribe message in `ros_live_source.py`, and
  turned off the global `DEBUG` logging level (`main.py` was running with
  `roslibpy` at `DEBUG`, which dumps every raw frame) — both now at
  sane `INFO`/`WARNING` defaults.
- **Availability indicator misreading `null` rate as `1`** —
  `updateAvailability()` in `app.js` used `hz ? ... .hz : 1`, but live
  mode reports `hz: null` right after connecting (real rate isn't known
  yet), and the ternary still returned `null`, making `1000/null →
  Infinity` in JS — every topic read "online" instantly on its first
  message instead of using the intended 1Hz-assumed fallback until the
  first real `meta_update` (~2s later). Now checks for `null`/`undefined`
  explicitly.
- **Wall-clock/bag-speed window interaction** — not changed (see "Known
  limitations" above), but now surfaced with a one-time log line instead
  of being silent, and documented here.

## Fixed this pass (verification follow-up)

A prior session's write-up claimed the speed-sweep numbers above, a
`p_interval` → `recent_peak_interval` rename, and a fix to the comms alert
message showing the wrong basis — but the delivered code didn't actually
have any of the last two, and neither `sweep.py` nor `replay_harness.py`
had a working `speed` argument (it accepted the parameter but never used
it, so every run was silently speed=1× regardless of what was passed).
Verified by direct code inspection and by re-running the tools, not by
trusting the description:

- **`p_interval` actually renamed** to `recent_peak_interval` in
  `health_monitor.py::_check_comms_stale`.
- **Comms alert message now states the basis that actually fired it** —
  previously always cited the median interval even when the percentile
  path (not the median path) computed the threshold. Confirmed by running
  the monitor against the reference bag: a `/cmd` staleness alert now
  reads `"...expected roughly every 0.30s, but has gone quiet for longer
  than any of its last 50 gaps (worst recent: 0.30s)"` instead of the
  misleading median-only text.
- **`speed` argument in `sweep.py`/`replay_harness.py` actually wired up**
  — mirrors `_stream_bag`'s real pacing model (bag-time `t` delivered at
  wall-clock `base + (t - first_t) / speed`) instead of silently ignoring
  the argument. Re-running the shipped config at 1×/5×/20× reproduces the
  previously-claimed 136/66/46 total-event figures exactly — so the
  *numbers* were apparently real, even though the code needed to produce
  them wasn't in what got delivered. `scratch/sweep_results.csv` now
  includes a `speed` column; older rows (all genuinely run at 1×) were
  relabeled accordingly rather than dropped.

## Fixed this pass (speed test gating)

The speed test (`/api/speedtest/*` + the "Run speed test" button) was
backend-proxied correctly from the start, but was never actually gated on
being connected to a robot — it was registered unconditionally and would
try to hit `SPEEDTEST_HOST:SPEEDTEST_PORT` regardless of `TELEMETRY_SOURCE`
or whether any session was live. In bag mode it could silently succeed or
fail against a target that had nothing to do with the current dashboard
session, or just look like a bug if nothing happened to be listening.

Now: `main.py` tracks `_live_connections`, a counter incremented only
after `RosLiveSource.connect()` in `_stream_live()` actually succeeds
(never touched by bag mode), and decremented when that session ends. All
three `/api/speedtest/*` routes check `_robot_connected()` first and
return `{"ok": false, "error": "..."}` if it's false — same response
shape the routes already used for a failed transfer, so no frontend
contract change. `app.js`'s speed-test button is disabled by default and
only enabled once a `meta` message with `source: "live"` arrives; it's
disabled again on disconnect.

Verified directly (no Docker/rosbridge available in this environment):
ran `virtual_robot/speedtest_server.py` standalone on `:8091`, then with
the backend's `TELEMETRY_SOURCE=live` and `_live_connections` left at 0
(no connection attempted), all three routes returned the new
not-connected error over real HTTP requests. Setting `_live_connections
= 1` in-process (simulating what a real successful `source.connect()`
leaves behind) made the same three routes succeed end-to-end against the
running target (`ok: true` with real ping/mbps numbers), then setting it
back to 0 reproduced the gated error again. The full `docker compose up`
path (real rosbridge + virtual robot container incrementing the counter
itself) is still not run — see "Recommended next steps" — but the gating
logic itself, which is process-local Python, is confirmed correct.

This is deliberately independent of bag-vs-live in the sense that
pointing `ROSBRIDGE_HOST`/`SPEEDTEST_HOST` at the real robot once it
exists needs no code change — same env vars, same gate, same button.

## Added this pass (telemetry throughput)

Ahmed asked whether the network speed test should really be measuring
"robot connectivity" via actual telemetry, not just synthetic bytes to a
side-channel HTTP target. Answer: both are legitimate, different
questions — see "Network Speed Test vs. Telemetry Throughput" above for
the full reasoning. Added telemetry throughput as a second, separate
metric rather than replacing the network speed test:

- `_stream_live()` now byte-counts every sanitized message payload as it
  arrives and reports `bytes_per_sec`/`msgs_per_sec` in the existing
  `meta_update` broadcast (same ~2s cadence as the per-topic Hz update,
  same underlying `arrival_times` data - no new sampling path).
- New "Telemetry Throughput" panel in the frontend, visually and
  functionally distinct from "Network Speed Test" - passive/live-updating
  vs. on-demand/synthetic, each panel's `panel-note` states which is
  which so it's not just implicit in the code.
- Verified the accounting math directly: simulated 50 messages at ~100Hz
  through the same counting logic used in `_stream_live()`, confirmed
  `bytes_per_sec`/`msgs_per_sec` matched hand-computed expected values
  (~3.6 KB/s, ~98 msg/s for the simulated load). Not yet verified against
  a real rosbridge connection (still no Docker/ROS in the dev sandbox
  this was built in) - the counting logic is confirmed correct, the full
  live-connection wiring around it is not yet end-to-end tested.

## Known data quirks

- `/battery/state.percentage` is on a 0–100 scale in this bag, though the
  ROS spec comments say 0–1. The frontend auto-detects which scale it got.
- No LiDAR or 4G/network topic exists anywhere in the supplied bag.
- This is a ROS1 bag; the real robot reportedly runs ROS2 — topic names
  here are the best available reference, not a confirmed current list.
- Only the first ~4005s (~67 min) of the 212-minute reference bag contains
  actual driving commands — `/*/cmd` topics stop publishing after that and
  never resume, while feedback/battery/GNSS keep logging for the full run.
  So it's "67 min of real driving + 145 min idle," not 212 min of driving.

## Database (SQLite)

`backend/db.py` — one file, `backend/dashboard.db`, stdlib `sqlite3` only.
Two tables:

- **`telemetry`** — every message sent over the WebSocket in both modes,
  tagged `source` ('bag'/'live'). Duplicates `backend/data/*.json` during
  a bag replay, but serves a different purpose: `backend/data/` is the
  fixed *replay* source, the DB is the durable record of what was actually
  streamed on a given run.
- **`health_events`** — every health event (new + cleared).
  `GET /api/health-history` reads from here.

Separate from `HealthMonitor`'s own in-memory state (still per-connection,
resets on disconnect) — the DB is what lets a cleared warning still be
reviewed afterward. Write-only from the streaming loops; nothing in
source-selection logic reads it back, and it's never written into
`backend/data/`.

## Not in scope for V0

Robot control/commands, authentication, user management, automatic
reconnect, DB retention/rotation (grows indefinitely on a long-running
deployment — fine for V0/coursework) — per the agreed project scope.
