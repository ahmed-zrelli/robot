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
for offline replay. **Robot address**: Settings → *Robot connection* (stored in
the DB; *Test connection* checks the port first; *Save & reconnect* switches
every open dashboard live). Without a saved value, `ROSBRIDGE_HOST` /
`ROSBRIDGE_PORT` are used, then localhost:9090. `SPEEDTEST_PORT` (default 8091)
idem for the speed test; `SPEEDTEST_HOST` pins it to another machine.

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
python -m unittest test_infer test_schema_contract test_persistence test_health_monitor test_auth   # fast suite (~15s; test_auth needs httpx)

$env:PGUARD_GOLDEN = '1'
python -m unittest test_health_monitor.TestGoldenReplay    # full-bag replay (~15 min)
```

```bash
node --check frontend/app.js
node scratch/test_frontend_logic.cjs        # history grouping/filter rules
```

## Security (four layers - defense in depth)

rosbridge has no authentication: anything that reaches port 9090 can read every
topic and publish motor commands. Four independent layers:

| Layer | Where | How to use |
|---|---|---|
| **rosbridge firewall** (prevention) | `virtual_robot/entrypoint.sh` | On by default in `docker-compose.yml`: only the Docker gateway (= the backend) may reach `:9090`. Open it for debugging with `$env:ROSBRIDGE_ALLOWED_SOURCES = ""` before `docker compose up`. On the real robot this is a router/firewall rule. |
| **Security watch** (detection) | `backend/health_monitor.py` (kind `security`) | Always on in live mode: unknown rosbridge client, motor command above 240 rad/s (data-derived: never reached in the reference bag), topic appearing mid-session. **Who is trusted** (priority order): the trusted-IP list on the **Settings** page (stored in the DB, applies immediately) → `SECURITY_TRUSTED_IPS=ip1,ip2` → clients connected at session start (automatic; trusts an attacker already connected, so configure a list). |
| **Dashboard access list** (prevention) | `backend/auth.py` (`require_login`), Settings → *Dashboard access* | Which **computers** may open the dashboard (single IPs or networks like `192.168.1.0/24`, stored in the DB). Others get a **bare `Not Found` (404) on every path** - no login page, no branding, nothing revealing a dashboard exists; the WebSocket handshake is refused without reason. Logged server-side. Empty list = everyone reaches the login page. `127.0.0.1` is always allowed (recovery), and a change that would exclude your own computer is refused. Only matters once the backend listens on the network (`--host 0.0.0.0`); by default it listens on `127.0.0.1` only. Note: Docker Desktop relays containers on the same PC through `127.0.0.1`, so they count as local. |
| **Dashboard login** (prevention) | `backend/auth.py`, `frontend/settings.html` | Operator **accounts in the DB** (table `users`, PBKDF2-SHA256 600k iterations, per-user salt - no plaintext). Manage them on **Settings** (top-right of the dashboard). **Roles**: *admin* (accounts, roles, access lists, robot connection, policy, audit log) and *operator* (dashboard, acks, own password only - their Settings page is just "My account"). Enforced in the middleware for every request, deny-by-default for `/api/settings*` and `/api/audit`; the role is re-read from the DB on each request so a demotion applies at once; the first account is always admin and the last admin can't be demoted or deleted. Accounts created before roles existed were migrated to admin. No account = open dashboard (first run): create the first one in Settings. One-time migration: if no account exists and `DASHBOARD_PASSWORD` is set, an `admin` account is created from it at startup; the variable is ignored afterwards. Protects page, API, reports, Settings and the WebSocket. |

**Security policy** - Settings → *Security policy*, stored in the DB
(`settings` table, rules in `backend/policy.py`): failed logins before lockout
(5), counting window (300 s), lockout (60 s), session length (8 h), minimum
password length (8), Security-watch max motor command (240 rad/s) and alert
hold (60 s). Each has hard bounds - the policy can be tightened but never
weakened below its floor; out-of-range values are refused, never clamped, and
one bad value refuses the whole save. PBKDF2 iterations and the detection
thresholds pinned by the golden test stay in code on purpose.

**Audit log** (traceability) - `backend/db.py` `audit_log`, Settings → *Audit log*:
logins, failed logins, lockouts, blocked computers (max one row per IP per
minute), password/account/list changes, alert acks, server starts - with user,
IP and time. Passwords are never logged. Append-only from the app, and
hash-chained (each row stores SHA-256 of the previous row's hash + its own
fields): the panel re-verifies the whole chain and names the first edited or
deleted row. Limit: someone with write access to the DB file could recompute
the chain - an HMAC key kept outside the DB would close that.

**Alert acknowledgements** are stored in the DB (`alert_acks`) with the
operator's name, time and IP, shared by every open dashboard (`/api/acks`,
polled every 4 s), and close automatically when the alert clears. When the last
dashboard showing an alert disconnects, its ack closes after 150 s unless a
connection re-detects it (a reload keeps the ack; a frozen sensor takes ~75 s
to re-detect). A backend restart closes all open acks.

Demo (virtual robot only - it publishes a forged motor command):

```bash
$env:ROSBRIDGE_ALLOWED_SOURCES = ""; docker compose up -d --force-recreate virtual-robot   # firewall OFF
docker compose --profile attack run --rm attacker        # alerts fire in the dashboard
Remove-Item Env:ROSBRIDGE_ALLOWED_SOURCES; docker compose up -d --force-recreate virtual-robot   # firewall ON (default)
docker compose --profile attack run --rm attacker        # "BLOCKED - Connection refused"
```

Opening the dashboard to the network (e.g. control-room PCs), the hardened way:

```powershell
# 1. In Settings -> Dashboard access, add the allowed PCs/network FIRST (from this PC).
# 2. Start without the "server: uvicorn" fingerprint header:
python -m uvicorn main:app --host 0.0.0.0 --port 8123 --no-server-header
# 3. Make the port not answer AT ALL for everyone else (OS firewall, admin PowerShell):
New-NetFirewallRule -DisplayName "PGuard dashboard" -Direction Inbound -Protocol TCP -LocalPort 8123 -RemoteAddress 192.168.1.0/24 -Action Allow
```

The app can only answer blocked computers with a bare 404; the firewall rule is
what makes the port silent (same idea as the iptables rule on the robot).

Not covered yet (production): TLS (`https`/`wss`), rosauth, admin/operator
roles, an HMAC key for the audit chain, and blocking - not just detecting -
forged commands on the robot. Tests use a throwaway DB (`test_auth.py`); set
`PGUARD_DB_PATH` to run a deployment on its own database file.

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
