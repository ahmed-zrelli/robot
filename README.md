# Outdoor Robot Monitor

A monitoring-only dashboard for an outdoor robot — battery, motors,
GPS, and automated health/fault detection, built ahead of having
direct access to the real robot. "V0" = first working version; no
robot commands or control, read-only telemetry only.

## Overview

Three pieces:

- **Backend** (`backend/`) — a Python server that gets robot telemetry
  from one of two sources (see below), stores it in a local database,
  runs it through health checks, and streams it to the browser over a
  WebSocket as `{type:'msg', topic, t, data}` messages.
- **Frontend** (`frontend/`) — the browser dashboard that displays it.
- **Virtual robot** (`virtual_robot/`) — an optional Docker container
  that simulates a real robot for testing, using **ROS** (Robot
  Operating System — the standard robotics framework this robot uses)
  and a **rosbag** (a recorded file of real robot data).

There's no real robot connected yet, so the backend gets its data one
of two ways:
- **Bag replay** — reads pre-extracted telemetry from `backend/data/`.
  No Docker needed. Default mode.
- **Live mode** — connects to the virtual robot container (or, later,
  the real robot) over **rosbridge**, ROS's WebSocket bridge, and asks
  it live what topics exist via `rosapi` (ROS's built-in introspection
  service) — nothing hardcoded.

Swapping the virtual robot for the real one later should only require
changes in `backend/main.py` — the frontend and wire format don't
change.

## Project structure

```
backend/          Python server (FastAPI), health checks, database
frontend/          Browser dashboard (HTML/JS)
virtual_robot/     Docker container simulating the robot (ROS1 + rosbridge)
bag/               Drop a .bag file here for the virtual robot / extraction
```

## Requirements

- Python 3.9+
- Docker (only needed for virtual robot / live mode — not for bag replay)
- Commands below use Windows PowerShell syntax (`$env:VAR="value"`);
  on Mac/Linux use `export VAR=value` instead.

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
| GNSS (GPS position/fix) | Live — `enova_msgs` GnssFix/GnssStatus |
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
2. **Statistical anomalies (WARNING)** — each signal (e.g. battery
   voltage) is compared to its own recent normal range (last 3 minutes).
   A value flagged as an outlier means it's unusually far from that
   recent range — not compared against a fixed safe range, just against
   what's been normal for that signal recently.
3. **Commanded-vs-measured velocity residual (WARNING)** — per motor,
   an unusually large gap between commanded and actual velocity
   (possible slip/stall/load issue).
4. **GNSS fix degradation (WARNING)** — flags when the fix drops below
   3D.

When a signal crosses into anomaly territory, that's logged as a **new**
issue (shows up in the health banner and Event History). When it goes
back to normal, that's logged as **cleared**. Checks 2 and 3 need 3
anomalous samples *in a row* to fire "new", and 3 normal samples in a
row to fire "cleared" — a single one-off spike doesn't trigger anything.

### How the baseline works

Each signal (e.g. battery voltage) is judged against its own recent
normal behavior — its "baseline". Two settings control this:

- **3-minute memory** — the baseline only remembers the last 3 minutes
  of samples for that signal. Older samples fall off automatically.
- **25-sample minimum** — the system won't judge a signal as
  normal/anomalous until it's seen at least 25 samples of it. This is
  about *count*, not time — a fast signal (like motor feedback, ~5/sec)
  hits 25 samples in a few seconds; a slow one (like battery, ~1/sec)
  takes about 25 seconds. It does **not** wait a fixed 3 minutes before
  starting.

**On connect, the system tries to skip the cold start:** it pulls each
signal's last 3 minutes of history from the database and loads it into
the baseline *before* any live data arrives. If there's no history in
the last 3 minutes (first run ever, or it's been a while since the last
session), it just starts empty and builds up from live data as normal
— no error, no special case.

This warm-up applies to hardware faults, statistical anomalies, and
GNSS checks. The velocity-residual check (commanded vs. measured speed)
does **not** use warm-up yet and always starts cold on reconnect.

One more note: the warm-up history is pooled across *all* past runs,
not filtered by which robot or environment it came from — there's
currently only one robot, so this hasn't mattered yet.

**Not yet handled:** normal driving (accelerating, turning) can shift
multiple motor signals together, the same way a real fault can — the
system can't yet tell the two apart.

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
- The virtual robot container only has `roboteq_msgs` and `enova_msgs`
  compiled in — `/nmea_sentence` won't work in live mode until
  `nmea_msgs` is added too. Bag-replay mode is unaffected.

## Database (SQLite)

`backend/dashboard.db`, stdlib `sqlite3`, two tables:
- **`telemetry`** — every streamed message, both bag and live mode.
- **`health_events`** — every health new/cleared event.

Also now read from (`db.recent_telemetry`) to warm-start health-monitor
baselines on connect. Grows indefinitely — no retention/rotation yet.

## Not in scope for V0

Robot control/commands, authentication, user management, automatic
reconnect, DB retention/rotation.

## Open questions

Facts about the real robot still unconfirmed, blocking full live
integration: RV55 router/modem access path; real robot's actual ROS
version/topic list (this bag is ROS1, robot reportedly ROS2); LiDAR
specifics; PC↔robot reachability/security model (rosbridge/MQTT/VPN/
etc.); `/coil_outputs`, `/nmea_sentence`, `motor_controller_msgs` —
unwired, unconfirmed.