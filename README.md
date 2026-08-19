# Outdoor Robot Monitor — V0

Monitoring-only dashboard for an outdoor robot. Currently replays a
recorded rosbag as if it were live telemetry; swapping in a real robot
later only touches `backend/main.py` — frontend and wire format
(`{type:'msg', topic, t, data}`) stay the same.

## Quick start (bag replay, no Docker)

```
cd backend
pip install -r requirements.txt
python -m uvicorn main:app --port 8123
```
Open `http://localhost:8123`, pick a playback speed, click **Connect**.
Uses the pre-extracted JSON in `backend/data/` — works out of the box.

## Virtual robot mode (Docker)

Instead of local JSON, point the dashboard at a Docker container that
plays the bag as live ROS1 topics over rosbridge — same discovery path
the backend will use with the real robot (topics/types found live via
`rosapi`, nothing hardcoded, nothing read from `backend/data/`).

1. Bag file goes in `bag/`.
2. Build and start the container: `docker compose up --build`
   (only builds the image + starts `sshd` — nothing ROS-related
   auto-starts, same as the real robot).
3. In a second terminal, start the robot software yourself:
   ```
   ssh robot@localhost -p 2222      # password: robot
   start_robot
   ```
   Starts `roscore`, `rosbridge` (`ws://localhost:9090`), and loops
   `rosbag play`. Stays attached — run it inside `screen`/`tmux`/`nohup`
   if you need it to survive closing the SSH session.
4. In a third terminal:
   ```
   cd backend
   pip install -r requirements.txt
   $env:TELEMETRY_SOURCE="live"     # Windows PowerShell
   python -m uvicorn main:app --port 8123
   ```
5. Open `http://localhost:8123`, click **Connect**.

No `TELEMETRY_SOURCE` set = defaults to bag replay.

**Useful commands inside the container** (same `ssh robot@localhost -p 2222`):
```
rostopic list
rostopic echo /battery/state
rostopic hz /left/front/feedback
rosbag info /bag/your_file.bag      # check what a bag actually contains
```

**Known constraint:** the container only has `roboteq_msgs` and
`enova_msgs` compiled in — `/nmea_sentence` (`nmea_msgs/Sentence`) won't
deserialize in live mode until `nmea_msgs` is added too. Bag-replay mode
is unaffected.

## Re-extracting from a raw bag (optional)

Raw `.bag` files aren't included (too large). To regenerate
`backend/data/*.json`: drop a `.bag` file into `bag/`, then run
`python backend/extract_bag.py`. Auto-discovers every topic in the bag
(no whitelist), overwrites `backend/data/*.json`.

## What's real vs. placeholder

| Panel | Status |
|---|---|
| Battery | Live — `/battery/state`, `/charging` |
| Motors (4x) + driver status/faults | Live — `roboteq_msgs` feedback/status |
| GPS/GNSS | Live — `enova_msgs` GnssFix/GnssStatus |
| Network / 4G | Placeholder — no topic in the bag, RV55 config unconfirmed |
| LiDAR | Placeholder — no topic in the bag |
| Speed test | Real, standalone browser test — not tied to robot telemetry, currently targets an old/wrong endpoint (left broken pending a real robot/router address) |
| Secure connect/disconnect | Opens/closes the WebSocket, clears telemetry on disconnect — not yet real secure transport to a robot |
| System Health | Live — see below |
| Event History | Live, session-scoped |
| Database | Live — see below |

## System Health / anomaly detection

A banner (OK / WARNING / CRITICAL) plus a live list of active issues.
Four detection layers:

1. **Hardware faults (CRITICAL)** — decoded directly from the roboteq
   motor controllers' status bitmask (OVERHEAT, OVERVOLTAGE,
   UNDERVOLTAGE, SHORT_CIRCUIT, EMERGENCY_STOP, etc).
2. **Statistical anomalies (WARNING)** — rolling per-signal baseline
   (mean/std) over the last 3 minutes of data, flagged when a new value
   is a statistical outlier (z-score > 3.5).
3. **Commanded-vs-measured velocity residual (WARNING)** — per motor,
   an unusually large gap between commanded and actual velocity
   (possible slip/stall/load issue).
4. **GNSS fix degradation (WARNING)** — flags when the fix drops below
   3D.

Checks 2 and 3 require 3 consecutive anomalous samples before firing
"new", and 3 consecutive normal samples before firing "cleared" —
avoids flagging single-sample spikes.

### How the 3-minute window and warm-up work

- **Window:** each signal's baseline (`RollingStat`) only keeps samples
  from the last `WINDOW_SECONDS = 180` (3 min), evicting anything older
  relative to the newest sample. This is time-based, not a fixed sample
  count — different topics publish at different rates, so a fixed count
  would mean "recent" covers a different amount of real time per signal.
  `MAX_SAMPLES = 900` is a separate safety cap on memory, not a
  redefinition of "recent".
- **Ready-to-judge threshold:** a signal isn't judged for anomalies
  until it has at least `MIN_BASELINE_SAMPLES = 25` samples in its
  window — this is a simple count floor, independent of the 3-minute
  window.
- **Warm-up on connect:** on every new connection, `db.recent_telemetry(topic, since, limit)`
  pulls rows from `dashboard.db` where `recorded_at >= now - 180s`
  (most recent first) and feeds them into the baseline *before* live
  samples start arriving — so a signal can already be past the
  25-sample floor at connect time instead of starting empty.
- **Clock used:** warm-up and live ingestion both use wall-clock
  `time.time()`, not the stream's own session-relative timestamp — this
  matters because mixing the two would make old warm-up rows look
  "recent" (or get evicted wrongly) depending on how the numeric ranges
  happened to overlap.
- **If there's no recent history:** warm-up simply seeds nothing if
  nothing's been recorded in the last 3 minutes (first run, or a long
  gap since the last session) — the signal just starts cold, same as
  before warm-up existed.
- **Pooling:** warm-up pools history across *all* past sessions/
  connections, with no filtering by robot identity, terrain, or
  conditions — there's no schema support for per-robot/per-environment
  scoping yet.
- **What's warmed up vs. not:** checks 1 (hardware faults), 2
  (statistical), and 4 (GNSS) warm-start from DB history. Check 3
  (velocity residual) still starts cold every reconnect — reconstructing
  historical commanded/measured pairs in order wasn't built yet.

**Not yet handled:** a real maneuver (acceleration/turning) can produce
the same kind of correlated multi-signal shift as a real fault — the
detector can't currently tell them apart.

## Known data quirks

- `/battery/state.percentage` is 0–100 scale in this bag, despite the
  ROS spec comment saying 0–1. Frontend auto-detects.
- `motor_temperature` is dead (constant −4.175°C) — health checks use
  `channel_temperature` instead.
- No LiDAR or 4G/network topic exists anywhere in the supplied bag.
- Bag is ROS1; real robot reportedly ROS2 — topic names are best
  available reference only.
- High-rate topics are decimated at extraction time to a UI-sane rate
  (bag-extraction-only, doesn't apply to live mode).

## Database (SQLite)

`backend/dashboard.db`, stdlib `sqlite3`, two tables:
- **`telemetry`** — every streamed message, both bag and live mode.
- **`health_events`** — every health new/cleared event.

Also now read from (`db.recent_telemetry`) to warm-start health-monitor
baselines on connect. Grows indefinitely — no retention/rotation yet.

## Not in scope for V0

Robot control/commands, authentication, user management, automatic
reconnect, DB retention/rotation.

## Open questions (ask supervisor)

RV55 router/modem access path; real robot's actual ROS version/topic
list; LiDAR specifics; PC↔robot reachability/security model
(rosbridge/MQTT/VPN/etc.); `/coil_outputs`, `/nmea_sentence`,
`motor_controller_msgs` — unwired, unconfirmed.