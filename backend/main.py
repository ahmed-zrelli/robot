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
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from ros_live_source import RosLiveSource
from health_monitor import HealthMonitor, MONITORED_FIELDS, WINDOW_SECONDS, MAX_SAMPLES
from enums import FAULT_BITS, FIX_TYPES, DIFF_SOURCES
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
    for topic in MONITORED_FIELDS:
        rows = db.recent_telemetry(topic, cutoff, MAX_SAMPLES)
        rows.reverse()  # oldest first, matches warm_up()'s expected order
        telemetry_by_topic[topic] = rows
    health.warm_up(telemetry_by_topic)

DATA_DIR = Path(__file__).parent / "data"
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

TELEMETRY_SOURCE = os.environ.get("TELEMETRY_SOURCE", "bag").lower()
ROSBRIDGE_HOST = os.environ.get("ROSBRIDGE_HOST", "localhost")
ROSBRIDGE_PORT = int(os.environ.get("ROSBRIDGE_PORT", "9090"))

# Speed-test target - deliberately separate config from ROSBRIDGE_HOST/PORT
# even though it defaults to the same host: network connectivity and robot
# telemetry are tested independently (see virtual_robot/speedtest_server.py
# and README "Connecting to the real robot"), and on the real robot these
# could end up being genuinely different addresses/ports. Override with
# SPEEDTEST_HOST/SPEEDTEST_PORT if so.
SPEEDTEST_HOST = os.environ.get("SPEEDTEST_HOST", ROSBRIDGE_HOST)
SPEEDTEST_PORT = int(os.environ.get("SPEEDTEST_PORT", "8091"))

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
    topic_meta = {
        topic: {"hz": rates.get(topic, 1), **panel}
        for topic, panel in PANEL_MAP.items()
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


@app.websocket("/ws/telemetry")
async def telemetry_ws(websocket: WebSocket):
    await websocket.accept()

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
            "fix_types": FIX_TYPES,
            "diff_sources": DIFF_SOURCES,
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
    health = HealthMonitor()
    _warm_up_health(health)
    coverage_logged = set()
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
            db.record_telemetry(session_id, "bag", topic, rel_t, data)
            for ev in health.ingest(topic, data, time.time()):
                db.log_event(session_id, topic, ev)
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
    health = HealthMonitor()
    _warm_up_health(health)
    coverage_logged = set()

    def on_message(topic, t, data):
        loop.call_soon_threadsafe(queue.put_nowait, ("msg", topic, t, data))

    def on_topics_discovered(discovered):
        loop.call_soon_threadsafe(queue.put_nowait, ("discovered", discovered, None, None))

    source = RosLiveSource(host=ROSBRIDGE_HOST, port=ROSBRIDGE_PORT)
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
        await websocket.send_json({
            "type": "error",
            "message": f"Could not reach virtual robot at {ROSBRIDGE_HOST}:{ROSBRIDGE_PORT} - {e}",
        })
        return

    # Only counted as "connected to the robot" once source.connect() above
    # has actually succeeded - a failed connect (the except branch) never
    # reaches here, so it never marks the robot as reachable.
    _live_connections += 1

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
    try:
        while True:
            kind, a, b, c = await queue.get()

            if kind == "discovered":
                discovered = a  # list of (topic, type)
                topics_meta = {}
                for topic, msg_type in discovered:
                    panel = PANEL_MAP.get(topic, {})
                    topics_meta[topic] = {"type": msg_type, "hz": None, **panel}
                await websocket.send_json({
                    "type": "meta",
                    "source": "live",
                    "session_id": session_id,
                    "duration_s": None,
                    "speed": 1.0,
                    "topics": topics_meta,
                    "fault_bits": FAULT_BITS,
                    "fix_types": FIX_TYPES,
                    "diff_sources": DIFF_SOURCES,
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
                db.log_event(session_id, topic, ev)
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

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        _live_connections -= 1
        source.close()


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


NOT_CONNECTED_ERROR = (
    "Not connected to the robot - open a live connection first "
    "(speed test only measures the current robot link, it doesn't make sense in bag mode)."
)


@app.get("/api/speedtest/ping")
async def speedtest_ping():
    """Round-trip time to the speed-test target (min of several small
    requests, same approach as the browser-direct version this replaced -
    min rather than mean/median because we want "best case reachable
    latency", not something inflated by one slow first-connection request
    or GC pause."""
    if not _robot_connected():
        return {"ok": False, "error": NOT_CONNECTED_ERROR}
    url = f"http://{SPEEDTEST_HOST}:{SPEEDTEST_PORT}/ping"
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
    if not _robot_connected():
        return {"ok": False, "error": NOT_CONNECTED_ERROR}
    url = f"http://{SPEEDTEST_HOST}:{SPEEDTEST_PORT}/down?bytes={SPEEDTEST_DOWNLOAD_BYTES}"
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
    if not _robot_connected():
        return {"ok": False, "error": NOT_CONNECTED_ERROR}
    url = f"http://{SPEEDTEST_HOST}:{SPEEDTEST_PORT}/up"
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
