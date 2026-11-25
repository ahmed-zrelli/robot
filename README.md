# Outdoor Robot Monitor — V0

Live monitoring dashboard for the PGuard surveillance robot (Enova Robotics,
ROS1). Two telemetry sources behind one wire format — the frontend never
knows which is active. ML anomaly detection (Isolation Forest + Autoencoder)
fused with rule-based health checks.

## Run it — bag mode (offline replay)

```bash
cd backend
pip install -r requirements.txt
python extract_bag.py          # once per new .bag (reads bag/*.bag, writes backend/data/)
$env:TELEMETRY_SOURCE = "bag"  # PowerShell (export TELEMETRY_SOURCE=bag on bash) - default is live!
python -m uvicorn main:app --port 8123
# open http://localhost:8123, pick a speed, Connect
```

Kiosk / wall screen: `http://localhost:8123/?autoconnect=1&speed=20` connects
on load (speed is ignored in live mode).

## Run it — live mode (virtual robot in Docker)

```bash
# 1. put the reference .bag in bag/, then:
docker compose up --build
# wait for rosbridge on :9090 (docker compose logs -f)

# 2. second terminal:
cd backend
pip install -r requirements.txt
$env:TELEMETRY_SOURCE = "live"     # PowerShell (export TELEMETRY_SOURCE=live on bash)
python -m uvicorn main:app --port 8123
# open http://localhost:8123, Connect (speed selector is ignored live)
```

```bash
docker compose exec virtual-robot bash   # poke around inside (rostopic list, rosmsg show ...)
```

No `TELEMETRY_SOURCE` set = live mode. Set `TELEMETRY_SOURCE=bag` explicitly
for offline replay. `ROSBRIDGE_HOST` / `ROSBRIDGE_PORT` (default localhost:9090)
point live mode anywhere — same vars reach the real robot later, no code change.
`SPEEDTEST_HOST` / `SPEEDTEST_PORT` (default 8091) idem for the speed test.

## Train / re-train the ML models

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r backend/requirements.txt
python ml/train.py              # reads backend/data, writes ml/artifacts/
```

Artifacts: `scaler.pkl`, `if_model.pkl`, `ae_model.pt`, `ae_model.onnx`,
`metadata.json`, `threshold_curve.csv`. Validated numbers: ~1.1% FPR,
0.1s fault delay, 0 events/h on normal data. Details: `PGuard_Handover_v9.md`,
`Phase4_Evaluation_Report.md`.

## Tests

```bash
cd backend
python -m unittest test_infer test_schema_contract test_persistence test_health_monitor   # fast suite (~10s)

$env:PGUARD_GOLDEN = '1'
python -m unittest test_health_monitor.TestGoldenReplay    # full-bag replay (~15 min)
```

```bash
node --check frontend/app.js
node scratch/test_frontend_logic.cjs        # history grouping/filter rules
```

## Real robot — connecting (wired Ethernet, on-site)

```bash
# on the onboard computer:
rospack find rosbridge_server        # is it installed?
rosnode list | grep rosbridge        # is it running?
ss -tlnp | grep 9090                 # listening?
```

```powershell
# from the laptop (no shell needed):
Test-NetConnection -ComputerName <robot-ip> -Port 9090
# then: $env:ROSBRIDGE_HOST = "<robot-ip>"
```

Still unconfirmed: ROS1 exact distro, motor-controller package, LiDAR
topic/type, whether rosbridge runs on the robot, what "secure" concretely
means. The robot runs ROS1 (confirmed); nothing else is assumed.

## Panels and where their data comes from

| Panel | Source |
|---|---|
| Battery | `/battery/state`, `/charging` |
| Motors / Encoders (4×) + driver faults | `/*/feedback`, `*_roboteq_driver/status`, `/*/cmd` |
| 3D Robot | procedural PGuard from spec dims (no mesh assets): wheels turn at measured velocity, faulted sides glow, beacon pulses on critical |
| GPS / GNSS | `/ublox/gnss_fix`, `/ublox/gnss_status`, `/ublox/rtk_correction` |
| Coil Outputs, NMEA | `/coil_outputs`, `/nmea_sentence` |
| Network Speed Test | synthetic probe to speed-test target (button, live only) |
| Telemetry Throughput | real ROS byte rate (live only, passive) |
| Network / 4G, LiDAR | **placeholders — no such topics in the bag** |
| System Health + Event History | `health_monitor.py` (rules) + `ml/infer.py` (category 9) |

Health event kinds: `hardware_fault` (controller bitmask, real classification),
`statistical` (unusual, not diagnosed), `residual` (commanded-vs-measured gap),
`gnss` (fix lost: {NO_FIX, DEAD_RECKONING, TIME_ONLY}), `sensor_health` vs
`sensor_failure` (suspect vs proven-dead sentinel), `comms` (topic silence),
`ml` (Isolation Forest OR Autoencoder, never escalates), `failure`
(escalated capability loss: mobility/localization/power/supervision). Full
semantics live in `backend/health_monitor.py`'s docstring — read that, not a
wiki. History supports severity/kind filters, incident grouping (display-only),
and JSON/HTML export via `/api/report`. Model facts: `/api/ml-status`.

Golden pins (`test_health_monitor.py::TestGoldenReplay`, full 212-min bag at
1×): statistical 33, residual 20, hardware_fault 10, gnss 12, sensor_health 1,
sensor_failure 4, comms 8, failure 27, ml 5. That test, not this list, is the
source of truth.

## Known limitations (deliberate, not oversights)

- Bag replay faster than 1× distorts wall-clock windows/thresholds (180s window
  covers ~1h of bag time at 20×) — validate at 1×, demo at any speed.
- Only ~67 of 212 bag minutes contain driving commands; the rest is idle.
- `/battery/state` here predates the `temperature` field; motor `temperature`
  is an unwired sentinel (−4.1754, proven dead); GNSS velocities/accuracies
  are unpopulated zeros; battery SoC gauge reads 0–100 despite the spec's 0–1.
- DB grows indefinitely (no retention in V0); golden replay takes ~15 min
  with ML wired; `docker compose up` was built but full-stack Docker runs
  happen on real machines, not in doc CI.

## Project layout

```
backend/      FastAPI + WebSocket (main.py), health_monitor.py, features.py,
              db.py (SQLite), ros_live_source.py, extract_bag.py, tests
frontend/     vanilla HTML/CSS/JS dashboard (app.js, index.html, style.css)
ml/           train.py, infer.py, artifacts/ (models + metadata.json)
virtual_robot/ ROS1 Noetic container (rosbag loop, rosbridge, relay, speed test)
scratch/      replay/sweep/validation harnesses (analysis tools, not the app)
bag/          reference .bag (too large to commit — dropped in by hand)
```

Handovers (`PGuard_Handover_v8.md`, `_v9.md`, `_v10.md`) and
`Phase4_Evaluation_Report.md` carry full history and numbers.
`TECHNICAL_NOTES.md` keeps the design reasoning (why wall-clock, why
split baselines, why vdrop, why no prediction) in one place.
