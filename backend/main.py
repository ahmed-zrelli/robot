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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from ros_live_source import RosLiveSource
from health_monitor import HealthMonitor, MONITORED_FIELDS, WINDOW_SECONDS, MAX_SAMPLES
from enums import FAULT_BITS, FIX_TYPES, DIFF_SOURCES
import db


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
    health = HealthMonitor()
    _warm_up_health(health)
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
        await websocket.send_json({"type": "eof"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass


# --------------------------------------------------------------- live mode
async def _stream_live(websocket: WebSocket, session_id: str):
    """Subscribes to the robot over rosbridge. Topic list/types come from
    the robot itself (rosapi), not from any local file. Per-topic rate is
    measured from real message arrivals and reported to the frontend once
    known - nothing here is precomputed or guessed."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    arrival_times = defaultdict(lambda: deque(maxlen=10))
    health = HealthMonitor()
    _warm_up_health(health)

    def on_message(topic, t, data):
        loop.call_soon_threadsafe(queue.put_nowait, ("msg", topic, t, data))

    def on_topics_discovered(discovered):
        loop.call_soon_threadsafe(queue.put_nowait, ("discovered", discovered, None, None))

    source = RosLiveSource(host=ROSBRIDGE_HOST, port=ROSBRIDGE_PORT)
    try:
        source.connect(on_message, on_topics_discovered=on_topics_discovered)
    except Exception as e:
        await websocket.send_json({
            "type": "error",
            "message": f"Could not reach virtual robot at {ROSBRIDGE_HOST}:{ROSBRIDGE_PORT} - {e}",
        })
        return

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
            now = time.monotonic()
            arrival_times[topic].append(now)

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
            # logic uses actual robot behavior, not a guess.
            if now - last_rate_broadcast > 2.0:
                last_rate_broadcast = now
                updates = {}
                for topic, times in arrival_times.items():
                    if len(times) >= 2:
                        span = times[-1] - times[0]
                        if span > 0:
                            hz = round((len(times) - 1) / span, 2)
                            updates[topic] = {"hz": hz, **PANEL_MAP.get(topic, {})}
                if updates:
                    await websocket.send_json({"type": "meta_update", "topics": updates})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
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


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
