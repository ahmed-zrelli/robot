"""
Robot Monitoring Dashboard - V0 backend.

Two telemetry sources, selected by TELEMETRY_SOURCE:

- "bag" (default): replays backend/data/*.json (extracted from a recorded
  rosbag by extract_bag.py) as if it were arriving live. Offline demo mode
  - useful with no robot/container running at all.

- "live": subscribes to a real ROS1 system over rosbridge (the virtual
  robot container, or later the real robot). Topic list and message types
  are discovered from the ROBOT ITSELF at connect time (via rosapi) - this
  mode has NO dependency on backend/data/ or anything extracted locally.
  See ros_live_source.py.

Either way the frontend only ever sees the same wire format:
{type:'msg', topic, t, data} - swapping sources doesn't touch the frontend.
"""
import asyncio
import json
import os
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

import aiohttp
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ros_live_source import RosLiveSource
from health_monitor import (
    HealthMonitor, MONITORED_FIELDS, WINDOW_SECONDS, MAX_SAMPLES,
    CMD_MOTOR,
)
from enums import (
    FAULT_BITS, STATUS_BITS, FIX_TYPES, DIFF_SOURCES, CARRIER_STATUS,
    POWER_SUPPLY_HEALTH, POWER_SUPPLY_TECHNOLOGY,
)
import db

import logging
logging.basicConfig(level=logging.INFO)
logging.getLogger("roslibpy").setLevel(logging.WARNING)  # roslibpy DEBUG dumps every raw frame - too chatty for normal runs
logger = logging.getLogger(__name__)


def _sanitize(value):
    """Recursively replace float NaN/Infinity with None.

    Python's json.dumps (and FastAPI's send_json) happily emit the bare
    tokens NaN/Infinity/-Infinity - valid Python, NOT valid JSON. JS's
    JSON.parse is strict and throws on them, so every message containing
    one was silently dropped by the frontend's ws.onmessage (see app.js -
    it has no try/catch around JSON.parse) and never reached the
    isFinite() guards that were already written to handle exactly this
    case. Real occurrence: /ublox/gnss_fix has 138/49,768 rows with
    lat/lon = NaN in the reference bag (mostly fixType=0, before the
    receiver has computed a position). Applied once here, at the single
    point where telemetry is about to leave the process (both to the
    frontend and to the DB), so every consumer downstream sees clean,
    spec-valid JSON and the frontend's existing isFinite() handling
    behaves as originally intended."""
    if isinstance(value, float):
        return None if (value != value or value in (float("inf"), float("-inf"))) else value
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    return value


# Bag-mode telemetry persistence is TIME-decimated (not skipped, not full
# rate). Why not skip: db.recent_telemetry() pooled across sessions is what
# warm_up() reads (stats AND residual replay), and list_sessions()/
# latest_session_id() deliberately cover telemetry-only sessions - writing
# nothing in bag mode would silently regress both on any DB without live
# history. Why not full rate: backend/data/*.json already preserves the
# complete replay source, so ~hundreds of thousands of rows per replay
# would be pure duplication. Time-based (not every-Nth): an Nth-row rule is
# replay-speed-dependent - at 1x a 1Hz topic keeps 180/50 = 4 rows per
# 180s window, below MIN_BASELINE_SAMPLES - while one row per topic per
# BAG_DB_WRITE_EVERY_S guarantees ~90 rows per topic per window at any
# speed, and bounds the total telemetry write rate at ~9 rows/s regardless
# of replay speed (vs ~470/s unbounded at 20x full rate). Live mode still
# records every message: there the DB IS the durable record (no
# backend/data/ equivalent), and future analysis/retraining may want full
# rate.
BAG_DB_WRITE_EVERY_S = 2.0


def _db_write_due(last_writes, topic, now, every=BAG_DB_WRITE_EVERY_S):
    """Per-topic persistence gate for decimated bag-mode writes. Returns
    True at most once per `every` wall-clock seconds per topic (first call
    for a topic always True), recording the write time. Pure logic, no I/O
    - unit-tested in test_persistence.py."""
    if now - last_writes.get(topic, 0.0) >= every:
        last_writes[topic] = now
        return True
    return False


def _log_new_coverage_warnings(health: HealthMonitor, already_logged: set):
    """Logs health.field_coverage_warnings() to the server console only -
    deliberately NOT sent to the frontend as a health_event (see
    health_monitor.py's field_coverage_warnings() docstring: a field
    that's never arrived at all is a config/schema problem discovered
    once at the start of a session, not a robot state that changes over
    time, so it doesn't belong in the same feed as sensor_health/comms/
    hardware_fault). `already_logged` is a per-session set of (topic,
    field) keys already printed - deliberately NOT keyed on the message
    text, since field_coverage_warnings() embeds the live running message
    count in the message for readability, which would make every call's
    string different and defeat the dedupe. Calling this repeatedly
    during a long live session logs each missing field exactly once."""
    for w in health.field_coverage_warnings():
        key = (w["topic"], w["field"])
        if key not in already_logged:
            logger.warning("[health coverage] %s", w["message"])
            already_logged.add(key)


def _warm_up_health(health: HealthMonitor):
    """Pre-fill a fresh HealthMonitor's statistical baselines from DB
    history before streaming starts (see health_monitor.py warm_up() and
    db.recent_telemetry()). Pooled across all sessions (Option A) -
    naturally excludes stale data since recent_telemetry filters by
    wall-clock recency."""
    cutoff = time.time() - WINDOW_SECONDS
    telemetry_by_topic = {}
    # MONITORED_FIELDS for the statistical baselines plus the /cmd topics
    # for the residual-baseline replay (see HealthMonitor._warm_up_residuals
    # - cmd rows hold the last-known command that feedback residuals are
    # computed against).
    for topic in list(MONITORED_FIELDS) + list(CMD_MOTOR):
        rows = db.recent_telemetry(topic, cutoff, MAX_SAMPLES)
        rows.reverse()  # oldest first, matches warm_up()'s expected order
        telemetry_by_topic[topic] = rows
    health.warm_up(telemetry_by_topic)

DATA_DIR = Path(__file__).parent / "data"
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

TELEMETRY_SOURCE = os.environ.get("TELEMETRY_SOURCE", "live").lower()
# Robot address (rosbridge host/port + speed-test port): Settings page ->
# DB `settings`, falling back to ROSBRIDGE_HOST / ROSBRIDGE_PORT /
# SPEEDTEST_HOST / SPEEDTEST_PORT env vars, then localhost:9090 / 8091.
# Read per connection via policy.robot() - never cached at import - so a
# change from Settings applies to the next connection, and open live
# connections are told to reconnect (see _reconnect_live_sessions).
# The speed test is still configured separately from telemetry (its own
# port, and SPEEDTEST_HOST can pin it to another machine): network
# connectivity and robot telemetry are tested independently.
import policy  # noqa: E402

# Which dashboard panel a topic feeds. This is UI ROUTING CODE, not data -
# it can't be discovered from a robot, since a topic name alone doesn't say
# "this is the battery panel". Any topic not listed here (bag or live mode)
# still streams over the WebSocket - nothing is silently dropped - it just
# has no panel to render into yet, same as the existing LiDAR/4G
# "not available yet" placeholders.
PANEL_MAP = {
    "/battery/state":               {"group": "battery"},
    "/charging":                    {"group": "battery"},
    "/left/front/feedback":         {"group": "motor", "motor": "left_front"},
    "/left/rear/feedback":          {"group": "motor", "motor": "left_rear"},
    "/right/front/feedback":        {"group": "motor", "motor": "right_front"},
    "/right/rear/feedback":         {"group": "motor", "motor": "right_rear"},
    "/left_roboteq_driver/status":  {"group": "motor_status", "side": "left"},
    "/right_roboteq_driver/status": {"group": "motor_status", "side": "right"},
    "/ublox/gnss_fix":              {"group": "gps"},
    "/ublox/gnss_status":           {"group": "gps"},
    "/ublox/rtk_correction":        {"group": "gps"},
}


# ---------------------------------------------------------------- bag mode
def _load_bag_topic_meta():
    """Bag mode only: rate per topic computed from the local extraction
    (message count / recording duration) - correct for whatever bag was
    last extracted. Never used in live mode."""
    manifest = json.loads((DATA_DIR / "manifest.json").read_text())
    duration = manifest["duration_s"]
    rates = {}
    for topic, fname in manifest["topics"].items():
        count = len(json.loads((DATA_DIR / fname).read_text()))
        rates[topic] = round(count / duration, 2) if duration else 0
    # Expose topic metadata to the dashboard. PANEL_MAP remains the
    # specialized-dashboard routing map; the frontend decides whether a topic
    # belongs in an existing panel or gets its own panel.
    topic_meta = {}
    for topic in manifest["topics"]:
        panel = PANEL_MAP.get(topic, {})
        topic_meta[topic] = {
            "hz": rates.get(topic, 0),
            "type": manifest.get("topic_msgtype", {}).get(topic, "unknown"),
            "mode": "on_change" if topic.endswith("/cmd") else "continuous",
            **panel,
        }
    return topic_meta, manifest, duration


def load_events():
    manifest = json.loads((DATA_DIR / "manifest.json").read_text())
    events = []
    for topic, fname in manifest["topics"].items():
        msgs = json.loads((DATA_DIR / fname).read_text())
        for m in msgs:
            t = m.pop("_t")
            events.append((t, topic, m))
    events.sort(key=lambda e: e[0])
    return events, manifest["duration_s"]


if TELEMETRY_SOURCE == "bag":
    BAG_TOPIC_META, _, _ = _load_bag_topic_meta()
    EVENTS, DURATION = load_events()
else:
    BAG_TOPIC_META, EVENTS, DURATION = {}, [], None

app = FastAPI()

# ---------- dashboard login (see auth.py; on as soon as one account exists in the DB) ----------
import auth  # noqa: E402

app.middleware("http")(auth.require_login)


@app.get("/login")
async def login_form():
    if not auth.enabled():
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/", status_code=303)
    return auth.login_page()


@app.post("/login")
async def login_submit(request: Request):
    return await auth.handle_login(request)


@app.post("/logout")
async def logout(request: Request):
    return auth.handle_logout(request)


@app.get("/api/auth-status")
async def auth_status(request: Request):
    return {"enabled": auth.enabled(),
            "logged_in": auth.valid_token(request.cookies.get(auth.COOKIE)),
            "user": auth.current_user(request),
            "role": auth.current_role(request) if auth.valid_token(request.cookies.get(auth.COOKIE)) else None}


# Settings (accounts + Security-watch trusted IPs), stored in SQLite and
# managed from frontend/settings.html. All behind the login middleware.
@app.get("/api/settings")
async def settings_get(request: Request):
    return auth.settings_state(request)


@app.post("/api/settings/password")
async def settings_password(request: Request):
    return await auth.change_password(request)


@app.post("/api/settings/users")
async def settings_add_user(request: Request):
    return await auth.add_user(request)


@app.post("/api/settings/users/{username}/role")
async def settings_set_role(request: Request, username: str):
    return await auth.set_role(request, username)


@app.delete("/api/settings/users/{username}")
async def settings_delete_user(request: Request, username: str):
    return auth.delete_user(request, username)


@app.post("/api/settings/trusted-ips")
async def settings_add_ip(request: Request):
    return await auth.add_trusted_ip(request)


@app.delete("/api/settings/trusted-ips/{ip}")
async def settings_remove_ip(request: Request, ip: str):
    return auth.remove_trusted_ip(request, ip)


@app.post("/api/settings/dashboard-ips")
async def settings_add_dashboard_ip(request: Request):
    return await auth.add_dashboard_ip(request)


@app.delete("/api/settings/dashboard-ips/{entry:path}")
async def settings_remove_dashboard_ip(request: Request, entry: str):
    # {entry:path} because CIDR entries contain '/' (e.g. 192.168.1.0/24)
    return auth.remove_dashboard_ip(request, entry)


# Policy (login limits, Security-watch limits) and robot address - DB-stored,
# validated against safe bounds in policy.py, every change audited.
@app.get("/api/settings/policy")
async def settings_policy_get():
    return auth.policy_state()


@app.post("/api/settings/policy")
async def settings_policy_update(request: Request):
    return await auth.update_policy(request)


@app.delete("/api/settings/policy/{key}")
async def settings_policy_reset(request: Request, key: str):
    return auth.reset_policy(request, key)


@app.post("/api/settings/robot")
async def settings_robot_update(request: Request):
    r = await auth.update_robot(request)
    if isinstance(r, dict) and r.get("ok"):
        r["reconnected"] = _reconnect_live_sessions(r["robot"])
    return r


@app.delete("/api/settings/robot")
async def settings_robot_reset(request: Request):
    r = auth.reset_robot(request)
    r["reconnected"] = _reconnect_live_sessions(r["robot"])
    return r


@app.post("/api/settings/robot/test")
async def settings_robot_test(request: Request):
    """Try a TCP connection to the given (not yet saved) address, so an
    operator can check it before switching every dashboard to it. Behind
    the login; only a connect, nothing is sent."""
    host, port, _st, problem = await auth.parse_robot_body(request)
    if problem:
        return JSONResponse({"ok": False, "error": problem}, status_code=400)
    t0 = time.monotonic()
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=3.0)
        w.close()
        return {"ok": True, "reachable": True, "ms": round((time.monotonic() - t0) * 1000, 1)}
    except Exception as e:
        return {"ok": True, "reachable": False, "error": type(e).__name__ + (f": {e}" if str(e) else "")}


# Alert acknowledgements (shared between operators, stored in the DB) and the
# hash-chained audit log, both behind the login middleware.
@app.get("/api/acks")
async def acks_get():
    return auth.acks_state()


@app.post("/api/acks")
async def acks_add(request: Request):
    return await auth.ack_alert(request)


@app.delete("/api/acks/{key:path}")
async def acks_remove(request: Request, key: str):
    # {key:path}: alert keys contain '/' (topic names)
    return auth.unack_alert(request, key)


@app.get("/api/audit")
async def audit_get(request: Request):
    return auth.audit_state(request)


auth.bootstrap_from_env()
# Monitors start fresh on every backend start and re-detect whatever is
# still wrong, so acks from the previous run must not hide those alerts.
_stale_acks = db.acks_resolve_all("restart")
auth.audit("server_started", detail=f"telemetry source {TELEMETRY_SOURCE}"
           + (f"; {_stale_acks} open acknowledgement(s) closed" if _stale_acks else ""))
if not auth.enabled():
    logger.warning("[auth] no operator account yet - dashboard is open to anyone who can reach it; "
                   "create one from Settings")


def _log_event(session_id, topic, ev):
    """Persist a health event and keep the shared acknowledgement state in
    step with it (an ack closes once no open dashboard has that alert)."""
    db.log_event(session_id, topic, ev)
    auth.note_alert(session_id, ev)


@app.websocket("/ws/telemetry")
async def telemetry_ws(websocket: WebSocket):
    peer = websocket.client.host if websocket.client else "?"
    if not auth.ip_allowed(peer):
        # Refuse the handshake itself (never accepted, no reason sent): a
        # blocked computer learns nothing about what runs here.
        auth.note_blocked(peer, "WebSocket /ws/telemetry")
        await websocket.close()
        return
    await websocket.accept()
    if not auth.valid_token(websocket.cookies.get(auth.COOKIE)):
        # Accept-then-close so the browser sees code 1008 (policy) and can
        # send the operator to /login, instead of an opaque handshake error.
        await websocket.close(code=1008, reason="authentication required")
        return

    session_id = uuid.uuid4().hex  # one per connection - isolates this
    # connection's telemetry/health rows in the DB from every other
    # connection's, past or future, while all still share one database.

    speed = 20.0
    q = websocket.query_params.get("speed")
    if q:
        try:
            speed = max(0.1, min(200.0, float(q)))
        except ValueError:
            pass

    try:
        await _stream_session(websocket, session_id, speed)
    finally:
        auth.end_alert_session(session_id)


async def _stream_session(websocket: WebSocket, session_id: str, speed: float):
    if TELEMETRY_SOURCE == "live":
        await _stream_live(websocket, session_id)
    else:
        await websocket.send_json({
            "type": "meta",
            "source": "bag",
            "session_id": session_id,
            "duration_s": DURATION,
            "speed": speed,
            "topics": BAG_TOPIC_META,
            "fault_bits": FAULT_BITS,
            "status_bits": STATUS_BITS,
            "fix_types": FIX_TYPES,
            "diff_sources": DIFF_SOURCES,
            "carrier_status": CARRIER_STATUS,
            "power_supply_health": POWER_SUPPLY_HEALTH,
            "power_supply_technology": POWER_SUPPLY_TECHNOLOGY,
        })
        await _stream_bag(websocket, speed, session_id)


async def _stream_bag(websocket: WebSocket, speed: float, session_id: str):
    # NOTE on speed vs. HealthMonitor's WINDOW_SECONDS: health.ingest() is
    # always given time.time() (real wall-clock), by design - see
    # db.recent_telemetry()'s docstring, this is what lets warm_up() and
    # live ingestion share one consistent clock. That's the right call.
    # The side effect: WINDOW_SECONDS=180 is 180 *wall-clock* seconds
    # always, regardless of `speed`. At the default 20x replay speed,
    # 180 wall-clock seconds = 3600s (1 hour) of bag time - the
    # statistical/residual baselines are effectively 20x wider (in bag-
    # time terms) than the README's "last 3 minutes" describes, and
    # MANEUVER_SETTLE_SECONDS/DEAD_SENSOR_MIN_SPAN_SECONDS etc. all
    # inherit the same stretch. This was an unexamined interaction, not a
    # deliberate choice - flagged here and in the README rather than
    # silently changed, since "should the window track bag-time instead
    # of wall-clock at non-1x speed" is a design call, not a bug fix.
    if speed != 1.0:
        logger.info(
            "[health] streaming bag at %sx - HealthMonitor's %ss window is wall-clock, "
            "so it covers ~%ss of bag time at this speed, not %ss (see README 'Known limitations').",
            speed, WINDOW_SECONDS, round(WINDOW_SECONDS * speed), WINDOW_SECONDS,
        )
    health = HealthMonitor(trusted_ips_provider=db.trusted_ip_set,
                           security_limits_provider=policy.security_limits)
    _warm_up_health(health)
    coverage_logged = set()
    last_db_write = {}  # topic -> last persisted monotonic t (decimation gate)
    try:
        sim_t0 = EVENTS[0][0]
        real_t0 = time.monotonic()
        idx = 0
        n = len(EVENTS)
        while idx < n:
            t, topic, data = EVENTS[idx]
            target_real_elapsed = (t - sim_t0) / speed
            now_real_elapsed = time.monotonic() - real_t0
            wait = target_real_elapsed - now_real_elapsed
            if wait > 0:
                await asyncio.sleep(min(wait, 0.2))
                continue
            rel_t = round(t - sim_t0, 3)
            data = _sanitize(data)
            await websocket.send_json({
                "type": "msg",
                "topic": topic,
                "t": rel_t,
                "data": data,
            })
            # Bag-mode persistence is time-decimated (see
            # BAG_DB_WRITE_EVERY_S): the full-fidelity replay source already
            # lives in backend/data/*.json; the DB keeps one row per topic
            # per interval - enough for session visibility and warm-up, at
            # ~1% of the write volume. Health events are all still logged.
            if _db_write_due(last_db_write, topic, time.monotonic()):
                db.record_telemetry(session_id, "bag", topic, rel_t, data)
            for ev in health.ingest(topic, data, time.time()):
                _log_event(session_id, topic, ev)
                await websocket.send_json({"type": "health_event", **ev})
            idx += 1
            if idx % 200 == 0:
                _log_new_coverage_warnings(health, coverage_logged)
        _log_new_coverage_warnings(health, coverage_logged)
        await websocket.send_json({"type": "eof"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass


# ---- robot connection state (for gating /api/speedtest/*, see below) ----
#
# A counter, not a bool: nothing stops two browser tabs each opening their
# own /ws/telemetry connection, and the speed test should stay usable as
# long as AT LEAST ONE live session actually reached the robot - it
# shouldn't flicker unavailable just because a second tab disconnected.
# Only _stream_live touches this (bag mode never increments it), which is
# exactly the "only work when connected to the robot" gate we want.
_live_connections = 0
_live_queues = {}   # session_id -> its event queue, to push a "reconnect" into


def _reconnect_live_sessions(new_robot):
    msg = (f"Robot address changed to {new_robot['host']}:{new_robot['port']} in Settings - "
           f"reconnecting.")
    for q in list(_live_queues.values()):
        q.put_nowait(("reconnect", msg, None, None))
    return len(_live_queues)


def _robot_connected() -> bool:
    return TELEMETRY_SOURCE == "live" and _live_connections > 0


# --------------------------------------------------------------- live mode
async def _stream_live(websocket: WebSocket, session_id: str):
    """Subscribes to the robot over rosbridge. Topic list/types come from
    the robot itself (rosapi), not from any local file. Per-topic rate is
    measured from real message arrivals and reported to the frontend once
    known - nothing here is precomputed or guessed."""
    global _live_connections
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    arrival_times = defaultdict(lambda: deque(maxlen=10))
    health = HealthMonitor(trusted_ips_provider=db.trusted_ip_set,
                           security_limits_provider=policy.security_limits)
    _warm_up_health(health)
    coverage_logged = set()

    def on_message(topic, t, data):
        loop.call_soon_threadsafe(queue.put_nowait, ("msg", topic, t, data))

    def on_topics_discovered(discovered):
        loop.call_soon_threadsafe(queue.put_nowait, ("discovered", discovered, None, None))

    robot = policy.robot()
    source = RosLiveSource(host=robot["host"], port=robot["port"])
    try:
        # Runs off the event loop: source.connect() blocks synchronously
        # while discover_topics_stable() polls rosapi/topics until the
        # topic set settles (see ros_live_source.py) - up to its
        # max_wait=15s default. Calling it directly on this coroutine
        # would stall every other WebSocket connection this worker is
        # handling for that whole window; run_in_executor keeps this
        # session's connect from blocking anyone else's. The on_message/
        # on_topics_discovered callbacks are unaffected - they already run
        # from roslibpy's own background thread and use
        # loop.call_soon_threadsafe, which is safe from any thread.
        await loop.run_in_executor(
            None,
            lambda: source.connect(on_message, on_topics_discovered=on_topics_discovered),
        )
    except Exception as e:
        source.close()  # stop roslibpy's background retries for this failed client
        await websocket.send_json({
            "type": "error",
            "message": f"Could not reach the robot at {robot['host']}:{robot['port']} - {e}",
        })
        return

    # Only counted as "connected to the robot" once source.connect() above
    # has actually succeeded - a failed connect (the except branch) never
    # reaches here, so it never marks the robot as reachable.
    _live_connections += 1
    _live_queues[session_id] = queue

    # Telemetry throughput accounting - see README "Network Speed Test vs
    # Telemetry Throughput" for why this is a SEPARATE metric from
    # /api/speedtest/*, not a replacement for it. This counts real ROS
    # message payload bytes actually received over rosbridge (measured the
    # same way arrival_times measures Hz - straight from real arrivals,
    # nothing synthetic/guessed), reset every broadcast window so each
    # meta_update reports "since the last update", not a cumulative total.
    telemetry_bytes_window = 0
    telemetry_msgs_window = 0

    last_rate_broadcast = 0.0
    last_rediscover = time.monotonic()
    try:
        while True:
            kind, a, b, c = await queue.get()

            if kind == "reconnect":
                # Robot address changed in Settings: tell the operator why,
                # then close with 1012 (service restart). The dashboard
                # treats it as a dropped link and reconnects by itself -
                # to the new address, since every connection reads it fresh.
                await websocket.send_json({"type": "error", "message": a})
                await websocket.close(code=1012, reason="robot address changed")
                return

            if kind == "discovered":
                discovered = a  # list of (topic, type)
                # Security watch: what exists at session start is the
                # reference; anything appearing later is flagged.
                health.set_topic_baseline([t for t, _ in discovered])
                topics_meta = {}
                for topic, msg_type in discovered:
                    panel = PANEL_MAP.get(topic, {})
                    topics_meta[topic] = {
                        "type": msg_type,
                        "hz": None,
                        "mode": "on_change" if topic.endswith("/cmd") else "continuous",
                        **panel,
                    }
                await websocket.send_json({
                    "type": "meta",
                    "source": "live",
                    "session_id": session_id,
                    "duration_s": None,
                    "speed": 1.0,
                    "topics": topics_meta,
                    "fault_bits": FAULT_BITS,
                    "status_bits": STATUS_BITS,
                    "fix_types": FIX_TYPES,
                    "diff_sources": DIFF_SOURCES,
                    "carrier_status": CARRIER_STATUS,
                    "power_supply_health": POWER_SUPPLY_HEALTH,
                    "power_supply_technology": POWER_SUPPLY_TECHNOLOGY,
                })
                continue

            # kind == "msg"
            topic, t, data = a, b, c
            data = _sanitize(data)
            now = time.monotonic()
            arrival_times[topic].append(now)

            # Byte-count the sanitized payload actually being delivered
            # (excludes the {type, topic, t} wrapper - just the ROS
            # message content itself, which is what a robot's own network
            # link actually has to carry).
            telemetry_bytes_window += len(json.dumps(data))
            telemetry_msgs_window += 1

            await websocket.send_json({
                "type": "msg",
                "topic": topic,
                "t": t,
                "data": data,
            })
            db.record_telemetry(session_id, "live", topic, t, data)
            for ev in health.ingest(topic, data, time.time()):
                _log_event(session_id, topic, ev)
                await websocket.send_json({"type": "health_event", **ev})

            # Every ~2s, report real observed rates (only for topics with
            # enough samples to estimate) so the frontend's stale/offline
            # logic uses actual robot behavior, not a guess. Telemetry
            # throughput piggybacks on the same cadence/message, computed
            # from the SAME real arrivals, not a separate synthetic probe.
            if now - last_rate_broadcast > 2.0:
                elapsed = now - last_rate_broadcast if last_rate_broadcast else 2.0
                last_rate_broadcast = now
                _log_new_coverage_warnings(health, coverage_logged)
                updates = {}
                for topic, times in arrival_times.items():
                    if len(times) >= 2:
                        span = times[-1] - times[0]
                        if span > 0:
                            hz = round((len(times) - 1) / span, 2)
                            updates[topic] = {"hz": hz, **PANEL_MAP.get(topic, {})}
                payload = {"type": "meta_update"}
                if updates:
                    payload["topics"] = updates
                if elapsed > 0:
                    payload["telemetry"] = {
                        "bytes_per_sec": round(telemetry_bytes_window / elapsed, 1),
                        "msgs_per_sec": round(telemetry_msgs_window / elapsed, 2),
                    }
                telemetry_bytes_window = 0
                telemetry_msgs_window = 0
                if "topics" in payload or "telemetry" in payload:
                    await websocket.send_json(payload)

            # Mid-session topic discovery (see REDISCOVER_EVERY_S): a node
            # that (re)starts after connect used to stay invisible until a
            # full reconnect. New topics ride the meta_update path the
            # frontend already merges (meta.topics), with hz unknown until
            # real arrivals land - same as initial discovery.
            if now - last_rediscover > REDISCOVER_EVERY_S:
                last_rediscover = now
                fresh = await loop.run_in_executor(None, source.poll_new_topics)
                if fresh:
                    logger.info("[live] discovered %d new topic(s) mid-session: %s",
                                len(fresh), [t for t, _ in fresh])
                    await websocket.send_json({
                        "type": "meta_update",
                        "topics": {
                            topic: {
                                "type": msg_type,
                                "hz": None,
                                "mode": "on_change" if topic.endswith("/cmd") else "continuous",
                                **PANEL_MAP.get(topic, {}),
                            }
                            for topic, msg_type in fresh
                        },
                    })
                    for ev in health.check_new_topics([t for t, _ in fresh], time.time()):
                        _log_event(session_id, ev["signal"], ev)
                        await websocket.send_json({"type": "health_event", **ev})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        _live_connections -= 1
        _live_queues.pop(session_id, None)
        source.close()


# ---------- ML model status (read-only panel data - see Phase 5) ----------
#
# Serves ml/artifacts/metadata.json essentials so the dashboard's ML panel
# can state what model is loaded, on what features/thresholds, and with
# what validated performance - without the frontend ever learning
# filesystem paths. Read once, served from memory (artifacts don't change
# under a running server). Fail-soft: without built artifacts this returns
# {"enabled": false} and the panel shows "model not loaded" - the rest of
# the dashboard (and HealthMonitor, which guards MLScorer the same way)
# is unaffected.
_ml_status_cache = None


def _load_ml_status():
    global _ml_status_cache
    if _ml_status_cache is not None:
        return _ml_status_cache
    meta_path = Path(__file__).parent.parent / "ml" / "artifacts" / "metadata.json"
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError) as e:
        logger.warning("[ml-status] artifacts unavailable: %s", e)
        _ml_status_cache = {"enabled": False}
        return _ml_status_cache
    thr = meta.get("thresholds", {})
    _ml_status_cache = _sanitize({
        "enabled": True,
        "schema_version": meta.get("schema_version"),
        "ml_features": meta.get("ml_features", []),
        "thresholds": {
            "if": {k: thr.get("if", {}).get(k) for k in ("idle", "drive")},
            "ae": {k: thr.get("ae", {}).get(k) for k in ("idle", "drive")},
            "fusion": thr.get("fusion", ""),
        },
        "gate": meta.get("test_gate", {}),
        "ae": {"window": meta.get("ae_window"),
               "stride": meta.get("ae_stride"),
               "arch": meta.get("ae_arch", {})},
        "grid_hz": meta.get("grid_hz"),
    })
    return _ml_status_cache


@app.get("/api/ml-status")
async def ml_status():
    """Model facts for the dashboard ML panel. Mode-independent (the model
    lives backend-side, not per-connection) and safe to call before ever
    connecting."""
    return _load_ml_status()


@app.get("/api/health-history")
async def health_history(session_id: str = None, limit: int = 500):
    """Logged health events (new + cleared), oldest first, capped to the
    most recent `limit`. Reads from db.py's SQLite store - independent of
    any live WebSocket connection, so history survives reconnects and
    server restarts.

    Scoped to one session by default: if session_id isn't given, this
    returns only the most recent session's events (so a fresh page load
    shows "what happened in the connection that's about to start/rejoin",
    not every connection anyone has ever made). Pass an explicit
    session_id to look at a past connection instead - see
    /api/sessions to find one. Pass session_id=all for the old
    everything-mixed-together behavior."""
    if session_id == "all":
        session_id = None
    elif session_id is None:
        session_id = db.latest_session_id()
    return {"session_id": session_id, "events": db.read_events(session_id=session_id, limit=limit)}


@app.get("/api/sessions")
async def sessions(limit: int = 50):
    """Past connections (sessions), most recent first, each with its
    start/end time and event count - lets a caller browse history from
    an earlier connection instead of only ever seeing the latest one."""
    return {"sessions": db.list_sessions(limit=limit)}


@app.get("/api/report")
async def report(session_id: str = None, format: str = "json"):
    """Session report for review/export (Phase 5): health events, per-topic
    telemetry coverage, ML model facts, and the detector config snapshot -
    everything a supervisor needs to review a session without replaying it.

    session_id defaults to the latest session (same rule as health-history).
    format=json returns the machine-readable compendium; format=html returns
    a single-file printable page (inline CSS, no JS). All robot-supplied
    strings are HTML-escaped in the HTML variant (same XSS lesson as the
    frontend renderers). Read-only: no writes, no streaming, no state."""
    import html as _html

    if format not in ("json", "html"):
        return {"ok": False,
                "error": "format must be 'json' or 'html'"}
    if session_id in (None, "all"):
        session_id = db.latest_session_id()
    import health_monitor as hm
    events = db.read_events(session_id=session_id, limit=10000)
    by_kind = {}
    for e in events:
        by_kind[e.get("kind", "?")] = by_kind.get(e.get("kind", "?"), 0) + 1
    body = {
        "session_id": session_id,
        "generated_at": time.time(),
        "event_count": len(events),
        "events_by_kind": by_kind,
        "events": events,
        "topics": db.session_topic_stats(session_id) if session_id else [],
        "ml": _load_ml_status(),
        "detector_config": {
            "z_threshold": hm.Z_THRESHOLD,
            "window_seconds": hm.WINDOW_SECONDS,
            "debounce_seconds": hm.DEBOUNCE_SECONDS,
            "min_baseline_samples": hm.MIN_BASELINE_SAMPLES,
            "gnss_degraded_fix": sorted(hm.GNSS_DEGRADED_FIX),
            "escalation_seconds": hm.ESCALATION_SECONDS,
            "maneuver_settle_seconds": hm.MANEUVER_SETTLE_SECONDS,
        },
    }
    if format == "json":
        return body

    def esc(v):
        return _html.escape("" if v is None else str(v), quote=True)

    def clock(ts):
        try:
            return time.strftime('%H:%M:%S', time.localtime(float(ts)))
        except (TypeError, ValueError):
            return "--:--:--"

    news = [e for e in events if e.get("state") == "new"]
    n_crit = sum(1 for e in news if e.get("severity") == "critical")
    n_warn = sum(1 for e in news if e.get("severity") == "warning")
    n_fail = sum(1 for e in news if e.get("kind") == "failure")
    span = ""
    if events and events[0].get("logged_at") and events[-1].get("logged_at"):
        span = f"{clock(events[0]['logged_at'])} → {clock(events[-1]['logged_at'])}"
    overall = "CRITICAL" if n_crit else ("WARNING" if n_warn else "NOMINAL")
    ocol = {"CRITICAL": "#C62828", "WARNING": "#E07B20", "NOMINAL": "#16A34A"}[overall]

    rows = "\n".join(
        f"<tr class='{esc(e.get('severity'))} {esc(e.get('state'))}'>"
        f"<td class='mono'>{clock(e.get('logged_at'))}</td>"
        f"<td><span class='st'>{esc(e.get('state'))}</span></td><td>{esc(e.get('kind'))}</td>"
        f"<td><span class='sev'>{esc(e.get('severity'))}</span></td><td><b>{esc(e.get('signal'))}</b></td>"
        f"<td>{esc(e.get('message'))}</td></tr>"
        for e in events
    ) or "<tr><td colspan='6' class='empty'>No health event recorded in this session.</td></tr>"
    trows = "\n".join(
        f"<tr><td class='mono'>{esc(t['topic'])}</td><td class='num'>{t['messages']}</td>"
        f"<td class='mono'>{t['first_t']} – {t['last_t']}</td></tr>"
        for t in body["topics"]
    )
    kinds = "\n".join(f"<span class='chip'>{esc(k)} <b>{v}</b></span>"
                      for k, v in sorted(by_kind.items()))
    cfg = "\n".join(f"<tr><td class='mono'>{esc(k)}</td><td class='mono'>{esc(v)}</td></tr>"
                    for k, v in body["detector_config"].items())
    logo = ""
    try:
        import base64
        logo_b64 = base64.b64encode((FRONTEND_DIR / "assets" / "enova.png").read_bytes()).decode()
        logo = f"<img src='data:image/png;base64,{logo_b64}' alt='Enova Robotics'>"
    except OSError:
        pass
    ml_on = body["ml"].get("enabled")
    html_doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>PGuard session report {esc(session_id)}</title>
<style>
*{{box-sizing:border-box}}body{{font-family:'Segoe UI',system-ui,sans-serif;max-width:1100px;margin:24px auto;color:#14202E;padding:0 16px;font-size:13px}}
.hdr{{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:18px 24px;border-radius:14px;background:linear-gradient(135deg,#10182A,#162033);color:#E8EDF2;border-bottom:3px solid #00B4B4}}
.hdr img{{height:44px;background:#fff;border-radius:6px;padding:3px 6px}}.hdr h1{{margin:0;font-size:19px;letter-spacing:.5px}}
.hdr p{{margin:3px 0 0;color:#8FA3B8;font-size:11.5px}}.state{{padding:6px 14px;border-radius:20px;font-weight:800;letter-spacing:1px;color:#fff;background:{ocol}}}
.cards{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin:16px 0}}.card{{border:1px solid #D5DEE8;border-radius:12px;padding:12px;text-align:center}}
.card .v{{font-size:24px;font-weight:800;font-family:Consolas,monospace;color:#008C8C}}.card .k{{font-size:9.5px;text-transform:uppercase;letter-spacing:1.2px;color:#5B6B7C}}
.card.c .v{{color:#C62828}}.card.w .v{{color:#E07B20}}
h2{{font-size:13px;text-transform:uppercase;letter-spacing:1.4px;color:#008C8C;margin:22px 0 8px;border-bottom:2px solid #E3EEF0;padding-bottom:5px}}
.chip{{display:inline-block;margin:0 6px 6px 0;padding:3px 10px;border-radius:12px;background:#EAF6F6;color:#10325A;font-size:12px}}
table{{border-collapse:collapse;width:100%;font-size:12px}}th{{background:#10325A;color:#fff;text-align:left;padding:6px 8px;font-weight:600}}
td{{padding:5px 8px;border-bottom:1px solid #E3E9EF;vertical-align:top}}tr:nth-child(even) td{{background:#F7FAFC}}
tr.critical td:first-child{{border-left:4px solid #C62828}}tr.warning td:first-child{{border-left:4px solid #E07B20}}tr.cleared td{{color:#7A8896}}tr.cleared td:first-child{{border-left-color:#16A34A}}
.sev,.st{{font-size:10px;font-weight:700;text-transform:uppercase;padding:1px 6px;border-radius:4px;background:#EEF2F6}}tr.critical .sev{{background:#FDECEC;color:#C62828}}tr.warning .sev{{background:#FFF4E8;color:#B45F14}}
.mono{{font-family:Consolas,monospace;font-size:11.5px}}.num{{text-align:right;font-family:Consolas,monospace}}.empty{{text-align:center;color:#7A8896;padding:18px}}
.foot{{margin-top:26px;text-align:center;color:#7A8896;font-size:11px}}
@media print{{body{{margin:0;max-width:none}}.hdr{{-webkit-print-color-adjust:exact;print-color-adjust:exact}}th,.state{{-webkit-print-color-adjust:exact;print-color-adjust:exact}}tr{{break-inside:avoid}}}}
</style></head><body>
<div class="hdr"><div style="display:flex;align-items:center;gap:14px">{logo}<div><h1>PGuard — Session health report</h1>
<p>Session <span class="mono">{esc(session_id)}</span> · generated {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(body['generated_at']))}{(' · events ' + span) if span else ''}</p></div></div>
<span class="state">{overall}</span></div>
<div class="cards">
<div class="card"><div class="k">Events logged</div><div class="v">{len(events)}</div></div>
<div class="card c"><div class="k">Critical (new)</div><div class="v">{n_crit}</div></div>
<div class="card w"><div class="k">Warnings (new)</div><div class="v">{n_warn}</div></div>
<div class="card c"><div class="k">Failure escalations</div><div class="v">{n_fail}</div></div>
<div class="card"><div class="k">ML layer</div><div class="v" style="font-size:16px;padding-top:6px">{'ENABLED' if ml_on else 'OFF'}</div></div>
</div>
<h2>Events by detector</h2><div>{kinds or '<span class="chip">none</span>'}</div>
<h2>Event log (oldest first)</h2>
<table><tr><th>time</th><th>state</th><th>detector</th><th>severity</th><th>signal</th><th>message</th></tr>
{rows}</table>
<h2>Topic coverage</h2>
<table><tr><th>topic</th><th>messages stored</th><th>stream span (s)</th></tr>
{trows}</table>
<h2>Detector configuration</h2>
<table><tr><th>parameter</th><th>value</th></tr>{cfg}</table>
<div class="foot">PGuard Robot Health Monitor · Enova Robotics · generated automatically from the dashboard database · read-only report</div>
</body></html>"""
    from fastapi.responses import Response
    return Response(content=html_doc, media_type="text/html")


# ---------- speed test (backend-proxied - see README "Connecting to the
# real robot" and virtual_robot/speedtest_server.py) ----------
#
# Deliberately backend-proxied rather than browser-direct: the frontend
# never learns the robot's address (same pattern as telemetry - it only
# ever talks to its own backend), and this avoids needing CORS headers on
# speedtest_server.py. Costs one extra hop (browser -> backend), which in
# the expected setup (backend running on the same machine as the browser)
# is loopback and doesn't meaningfully affect the measured numbers - the
# backend->robot leg is what's actually being measured either way.
#
# Each call opens its own short-lived aiohttp session rather than sharing
# one across requests - these are infrequent, user-initiated, one-shot
# tests, not a hot path, so the simplicity is worth more than reusing a
# connection pool here.

SPEEDTEST_PING_COUNT = 4
SPEEDTEST_DOWNLOAD_BYTES = 20_000_000
SPEEDTEST_DOWNLOAD_MAX_SECONDS = 5
SPEEDTEST_UPLOAD_BYTES = 10_000_000

# Minimum gap between two runs of the same speed-test endpoint. Each run
# moves tens of MB across the robot link on demand with no auth (V0 scope);
# without a cooldown, accidental double-clicks - or a curious user
# hammering the button - saturate the very link being measured. Returns the
# same {"ok": false} shape as a failed transfer, so no frontend change.
SPEEDTEST_COOLDOWN_S = 10.0
_speedtest_last_run = {}

# How often a live session re-polls rosapi for topics that appeared after
# connect (node restart, late publisher, bag loop restart). Single poll via
# RosLiveSource.poll_new_topics(), which subscribes to only the delta and
# never raises - a failed poll just retries next round. New topics arrive
# at the frontend through the existing meta_update path (hz: null until
# the first real arrivals, same as initial discovery).
REDISCOVER_EVERY_S = 30.0


NOT_CONNECTED_ERROR = (
    "Not connected to the robot - open a live connection first "
    "(speed test only measures the current robot link, it doesn't make sense in bag mode)."
)


def _speedtest_gate(name):
    """Shared pre-check for /api/speedtest/*: robot-connected AND outside
    the per-endpoint cooldown. Returns an error string, or None to proceed
    (recording this run's timestamp)."""
    if not _robot_connected():
        return NOT_CONNECTED_ERROR
    now = time.monotonic()
    last = _speedtest_last_run.get(name, 0.0)
    if now - last < SPEEDTEST_COOLDOWN_S:
        return (f"Cooldown - wait {SPEEDTEST_COOLDOWN_S - (now - last):.0f}s "
                f"between {name} runs (each run moves tens of MB on the robot link).")
    _speedtest_last_run[name] = now
    return None


@app.get("/api/speedtest/ping")
async def speedtest_ping():
    """Round-trip time to the speed-test target (min of several small
    requests, same approach as the browser-direct version this replaced -
    min rather than mean/median because we want "best case reachable
    latency", not something inflated by one slow first-connection request
    or GC pause."""
    err = _speedtest_gate("ping")
    if err:
        return {"ok": False, "error": err}
    r = policy.robot()
    url = f"http://{r['speedtest_host']}:{r['speedtest_port']}/ping"
    timings_ms = []
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            for _ in range(SPEEDTEST_PING_COUNT):
                t0 = time.perf_counter()
                async with session.get(url) as resp:
                    await resp.read()
                timings_ms.append((time.perf_counter() - t0) * 1000)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "ms": min(timings_ms)}


@app.get("/api/speedtest/download")
async def speedtest_download():
    """Streams SPEEDTEST_DOWNLOAD_BYTES (or SPEEDTEST_DOWNLOAD_MAX_SECONDS,
    whichever comes first - same bounded-stream approach as the
    browser-direct version this replaced) from the speed-test target and
    reports throughput. Only the download itself is timed - the initial
    connection/response-headers aren't counted against it."""
    err = _speedtest_gate("download")
    if err:
        return {"ok": False, "error": err}
    r = policy.robot()
    url = f"http://{r['speedtest_host']}:{r['speedtest_port']}/down?bytes={SPEEDTEST_DOWNLOAD_BYTES}"
    received = 0
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(url) as resp:
                t0 = time.perf_counter()
                async for chunk in resp.content.iter_chunked(65536):
                    received += len(chunk)
                    if time.perf_counter() - t0 > SPEEDTEST_DOWNLOAD_MAX_SECONDS:
                        break
                elapsed = time.perf_counter() - t0
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        return {"ok": False, "error": str(e)}
    if elapsed <= 0:
        return {"ok": False, "error": "elapsed time was zero - transfer too fast to measure"}
    mbps = (received * 8) / elapsed / 1_000_000
    return {"ok": True, "mbps": mbps, "bytes": received, "seconds": elapsed}


@app.post("/api/speedtest/upload")
async def speedtest_upload():
    """Generates SPEEDTEST_UPLOAD_BYTES of random data and times uploading
    it to the speed-test target's /up endpoint. Payload is built once
    up-front (not streamed from a generator) - at 10MB this is a trivial
    amount of memory, and a plain bytes object keeps the timed section
    unambiguous (upload transfer only, no interleaved data generation)."""
    err = _speedtest_gate("upload")
    if err:
        return {"ok": False, "error": err}
    r = policy.robot()
    url = f"http://{r['speedtest_host']}:{r['speedtest_port']}/up"
    payload = os.urandom(SPEEDTEST_UPLOAD_BYTES)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            t0 = time.perf_counter()
            async with session.post(url, data=payload) as resp:
                await resp.read()
            elapsed = time.perf_counter() - t0
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        return {"ok": False, "error": str(e)}
    if elapsed <= 0:
        return {"ok": False, "error": "elapsed time was zero - transfer too fast to measure"}
    mbps = (SPEEDTEST_UPLOAD_BYTES * 8) / elapsed / 1_000_000
    return {"ok": True, "mbps": mbps, "bytes": SPEEDTEST_UPLOAD_BYTES, "seconds": elapsed}


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
