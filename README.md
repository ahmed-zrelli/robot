# Outdoor Robot Monitor — V0

Monitoring-only dashboard prototype. No live robot yet, so it replays a
recorded rosbag over a WebSocket as if it were live telemetry. Swapping the
replay source for a real ROS2/rosbridge subscriber later only touches
`backend/main.py` — the frontend and wire format
(`{type:'msg', topic, t, data}`) don't change.

## Run it

`backend/data/*.json` (the already-extracted telemetry) is included in this
project, so the dashboard runs out of the box without the raw bag:

```
cd backend
pip install -r requirements.txt
python -m uvicorn main:app --port 8123
```

Open `http://localhost:8123`, pick a playback speed, click **Connect**.

## Virtual robot mode (Docker)

Instead of replaying local JSON, you can point the dashboard at a "virtual
robot" - a Docker container that plays the bag as live ROS1 topics over
rosbridge, so the backend subscribes the same way it eventually will to the
real robot.

**Live mode has zero dependency on `backend/data/` or anything extracted
locally.** The backend asks the *container* what topics exist and what type
each one is (via ROS's own introspection service, `rosapi`) at connect
time, and reports each topic's rate from real message arrivals it observes
- not from any file. You never need to have run `extract_bag.py` first, and
you don't know in advance what topics/data you'll get - same as the real
robot later.

1. Make sure your `.bag` file is in `bag/`.
2. Build and start the container:
   ```
   docker compose up --build
   ```
   This only builds the ROS1 Noetic image (`roboteq_msgs`/`enova_msgs`
   compiled in) and starts `sshd`. **Nothing ROS-related starts
   automatically** - no `roscore`, no `rosbridge`, no bag playback. This is
   deliberate: it mirrors a robot whose onboard software has to be launched
   manually rather than one that auto-starts on boot, so the workflow you
   practice here is the one you'll actually use on the real robot.
3. In a second terminal, SSH into the "robot" and start it yourself:
   ```
   ssh robot@localhost -p 2222      # password: robot
   start_robot
   ```
   This starts `roscore`, `rosbridge` (`ws://localhost:9090`), and loops
   `rosbag play` over your bag - and stays attached in your terminal
   (Ctrl+C stops bag playback). If you want it to keep running after you
   close the SSH session, run it inside `screen`/`tmux` or with `nohup` -
   same tradeoff you'll have on the real robot.
4. In a third terminal, run the backend in live mode:
   ```
   cd backend
   pip install -r requirements.txt
   $env:TELEMETRY_SOURCE="live"         # Windows PowerShell: $env:TELEMETRY_SOURCE="live"
   python -m uvicorn main:app --port 8123
   ```
5. Open `http://localhost:8123`, click **Connect**. The dashboard now
   discovers topics from the container live - watch the terminal/backend
   logs, or use your SSH session (below) to see exactly what's being
   published before it even shows up in the UI.

**If you click Connect before running `start_robot`**, it will fail/timeout
- same as it would against a real robot whose software hasn't been started
yet. That's expected, not a bug.

No `TELEMETRY_SOURCE` set = defaults to bag replay (reads `backend/data/`,
unchanged from before).

### SSH into the virtual robot

```
ssh robot@localhost -p 2222
```
Password: `robot`. This is the same login `start_robot` uses above - open a
second SSH session alongside it if you want to poke around while it's
running. ROS environment is pre-sourced, so once you're in:
```
rostopic list
rostopic echo /battery/state
rostopic hz /left/front/feedback
rosmsg show roboteq_msgs/Feedback
```
This is the actual discovery workflow you'll use against the real robot
later - the dashboard's own live-mode discovery works the same way
programmatically (`rosapi/topics`), just automated.

### What's still manual, and why

Which dashboard panel a topic renders into (`PANEL_MAP` in `main.py`) can't
be discovered from the robot - a topic name alone doesn't say "this is the
battery panel", that's a UI decision a human has to make. Any topic the
robot publishes that isn't in `PANEL_MAP` still streams over the WebSocket
in both bag and live mode - nothing is silently dropped - it just has no
panel to render into yet, same as the existing LiDAR/4G placeholders.

Extraction (`extract_bag.py`, bag mode only) auto-discovers every topic in
the bag too - no hardcoded topic list there either.

Live mode also has one build-time constraint: the container only has
`roboteq_msgs` and `enova_msgs` compiled in (see `virtual_robot/Dockerfile`).
This bag's `/nmea_sentence` topic (`nmea_msgs/Sentence`) won't deserialize
in live mode until `nmea_msgs` is added there too - bag-replay mode is
unaffected since it doesn't need ROS message definitions at all.

## Re-extracting from the raw bag (optional)

The raw `.bag` file itself is **not** included (too large for the repo/zip).
To regenerate `backend/data/*.json` yourself:

1. Drop your `.bag` file into `bag/` (filename doesn't matter — any single
   `.bag` file in that folder is picked up automatically).
2. Run:
   ```
   python backend/extract_bag.py
   ```
   Extracts every topic actually present in that bag (no topic whitelist),
   overwrites `backend/data/*.json`, and removes any stale JSON left over
   from a previous bag's topics that no longer exist. Only the topics
   listed in `PANEL_MAP` (`main.py`) actually render into a panel; anything
   else is still extracted and streamed, just has no panel yet.

## What's real vs. placeholder

| Panel | Status |
|---|---|
| Battery | Live — from `/battery/state`, `/charging` |
| Motors (4x) + driver status/faults | Live — from `roboteq_msgs` feedback/status topics |
| GPS/GNSS | Live — from `enova_msgs` GnssFix/GnssStatus |
| Network / 4G | **Placeholder** — no topic in the bag, RV55 config unconfirmed |
| LiDAR | **Placeholder** — no topic in the bag |
| Speed test | Real but standalone — browser-side test against a public CDN, not tied to robot telemetry (deliberately separate, per the project plan) |
| Secure connect/disconnect | Connect/Disconnect buttons open/close the WebSocket and clear all telemetry on disconnect. This is **not** yet the "secure" transport to the real robot — that depends on how the robot exposes data (rosbridge/MQTT/VPN/etc.), which is still unconfirmed. |
| System Health (anomaly detection) | Live — see dedicated section below for exactly what it does and doesn't do |

## System Health / anomaly detection

A banner at the top of the dashboard (OK / WARNING / CRITICAL) plus a live
list of active issues. Built with **no confirmed labeled fault dataset**
(still unresolved — see "Ask supervisor" list), so the scope is
deliberately limited to what's honestly buildable without one:

1. **Hardware faults (CRITICAL)** — decoded directly from the roboteq
   motor controllers' own status bitmask (`fault` field): OVERHEAT,
   OVERVOLTAGE, UNDERVOLTAGE, SHORT_CIRCUIT, EMERGENCY_STOP, etc. This is
   real classification, for free — the controller already knows this, the
   code just surfaces it. Edge-triggered, no debounce — it's a direct
   discrete report from the controller, not a noisy statistic.
2. **Statistical anomalies (WARNING)** — a rolling baseline (mean/std) per
   signal (battery voltage/current/temp, each motor's current/temp/supply
   voltage), flagged when a new value is a statistical outlier (z-score >
   3.5). The baseline covers a **3-minute time window**, not a fixed
   sample count — different topics publish at different rates, so a time
   window keeps "recent" consistent across every signal. This can only say
   "unusual compared to what we've recently seen" — it cannot say *what
   kind* of fault it is.
3. **Commanded-vs-measured velocity residual (WARNING)** — per motor, an
   unusually large gap between what was commanded and what was actually
   achieved (possible slip/stall/load issue). Same time-window treatment
   as (2).
4. **GNSS fix degradation (WARNING)** — flags when the fix drops below 3D.
   Edge-triggered, no debounce — same reasoning as (1).

**Debounce:** the statistical and residual checks (2 and 3) require 3
consecutive anomalous samples before firing a new event, and 3 consecutive
normal samples before clearing it — this kills single-sample flicker from
brief transients. Hardware faults and GNSS fix (1 and 4) are intentionally
not debounced, since delaying a real discrete state change isn't justified.

**Baseline warm-up:** a fresh baseline is still built per connection (no
in-memory state survives a reconnect), but on connect it's pre-filled from
the last 3 minutes of DB history across all past sessions, so detection
isn't judging off a near-empty window right after connecting. The residual
check (3) is not warmed up — it still starts cold every reconnect.

**Known limitation:** the maneuver-vs-fault distinction isn't handled. A
real turn or acceleration can produce genuine correlated shifts across
multiple motor signals, and so can a real fault cascading from one motor
to the others — the current checks can't yet tell those apart.

## Known data quirks found while building this

- This bag's `/battery/state.percentage` is populated on a 0–100 scale, even
  though the ROS `sensor_msgs/BatteryState` spec comments say 0–1. The
  frontend auto-detects which scale it got rather than assuming.
- No LiDAR or 4G/network topic exists anywhere in the supplied bag.
- The bag is a ROS1 bag; you said the robot currently runs ROS2. Topic names
  here are the best available reference, not a confirmed current topic list.

## Data decimation (display only)

High-rate topics (motor feedback, GNSS, roboteq status) are decimated at
extraction time to a UI-sane rate (see `DECIMATE_HZ` in `extract_bag.py`).
This is for readability in a browser, not a claim about the robot's real
publish rate — availability thresholds in the frontend use these same rates
so the 🟢/🟡/🔴 indicators stay consistent with what's actually streamed.

## Database (SQLite)

`backend/db.py` — one SQLite file, `backend/dashboard.db`, stdlib
`sqlite3` only, no new dependency. Two tables:

- **`telemetry`** — every `{topic, t, data}` message that goes out over
  the WebSocket, in **both** bag and live mode. `source` column ('bag'
  or 'live') distinguishes which run produced a row. This does duplicate
  `backend/data/*.json` while replaying a bag, but the two serve
  different purposes: `backend/data/` is the fixed source used to
  *replay*, the DB is the durable record of what was actually streamed
  out on a given run.
- **`health_events`** — every health event (new + cleared), replacing
  the old `event_log.py` JSONL file. `GET /api/health-history` reads
  from here now instead.

This is separate from `HealthMonitor`'s own in-memory state (still
per-connection, still resets on disconnect — unchanged): the DB is what
lets a warning/fault that already cleared still be reviewed afterward,
and now also keeps a durable record of raw telemetry itself, not just
health events.

Deliberately not written into `backend/data/` — that folder stays
bag-extracted telemetry only, and live mode still never *reads* from it
(see `main.py`); the DB is write-only from the streaming loops, nothing
in source-selection logic reads it back.

## Not in scope for V0

Robot control/commands, authentication, user management, automatic
reconnect, DB retention/rotation (the DB grows indefinitely on a
long-running deployment — fine for V0/coursework) — per the agreed
project scope.