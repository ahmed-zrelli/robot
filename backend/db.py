"""
SQLite storage for the dashboard - replaces the old JSONL-only event log.

Two tables:
- telemetry: every {topic, t, data} message streamed to the frontend,
  recorded in BOTH bag and live mode.
- health_events: every health_monitor.py event (new + cleared).

Both tables carry a `session_id` (one UUID per WebSocket connection,
assigned in main.py). Old sessions' rows are never deleted or touched by
a new connection - each connection only writes its own session_id, and
the frontend queries scoped to "current session" by default while still
being able to fetch any past session's data by id. This is what keeps
connections isolated from each other while still sharing one durable,
queryable database (session isolation view, not per-connection storage).

Single file, stdlib `sqlite3` only - no new dependency. One connection,
reused for the process lifetime, WAL mode so reads (API) and writes
(streaming loops) don't block each other.

Deliberately NOT backend/data/ - that folder stays bag-extracted
telemetry only, and live mode must never read from it (see main.py).
"""
import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "dashboard.db"

_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute(
    """
    CREATE TABLE IF NOT EXISTS telemetry (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  TEXT NOT NULL,
        source      TEXT NOT NULL,   -- 'bag' or 'live'
        topic       TEXT NOT NULL,
        t           REAL NOT NULL,   -- stream-relative timestamp (as sent to frontend)
        data        TEXT NOT NULL,   -- JSON-encoded message payload
        recorded_at REAL NOT NULL    -- wall-clock unix seconds
    )
    """
)
_conn.execute(
    "CREATE INDEX IF NOT EXISTS idx_telemetry_topic ON telemetry(topic)"
)
_conn.execute(
    "CREATE INDEX IF NOT EXISTS idx_telemetry_session ON telemetry(session_id)"
)
_conn.execute(
    """
    CREATE TABLE IF NOT EXISTS health_events (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        topic      TEXT NOT NULL,
        key        TEXT,
        kind       TEXT,
        severity   TEXT,
        signal     TEXT,
        message    TEXT,
        state      TEXT NOT NULL,   -- 'new' or 'cleared'
        logged_at  REAL NOT NULL    -- wall-clock unix seconds
    )
    """
)
_conn.execute(
    "CREATE INDEX IF NOT EXISTS idx_health_events_session ON health_events(session_id)"
)
_conn.commit()


def record_telemetry(session_id, source, topic, t, data):
    """Append one telemetry message, tagged to its session. Fire-and-
    forget: a DB failure must never take down telemetry streaming."""
    try:
        _conn.execute(
            "INSERT INTO telemetry (session_id, source, topic, t, data, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, source, topic, t, json.dumps(data), time.time()),
        )
        _conn.commit()
    except Exception:
        pass


def log_event(session_id, topic, ev):
    """Append one health event, tagged to its session."""
    try:
        _conn.execute(
            "INSERT INTO health_events "
            "(session_id, topic, key, kind, severity, signal, message, state, logged_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                topic,
                ev.get("key"),
                ev.get("kind"),
                ev.get("severity"),
                ev.get("signal"),
                ev.get("message"),
                ev.get("state"),
                time.time(),
            ),
        )
        _conn.commit()
    except Exception:
        pass


def read_events(session_id=None, limit=None):
    """Return logged health events, oldest first. If session_id is given,
    only that session's events are returned; otherwise all sessions
    (existing behavior). `limit` (if given) returns only the most recent
    N - still oldest-first within that slice."""
    cols = ["id", "session_id", "topic", "key", "kind", "severity", "signal", "message", "state", "logged_at"]
    col_list = ", ".join(cols)

    where = ""
    params = []
    if session_id:
        where = "WHERE session_id = ?"
        params.append(session_id)

    if limit:
        query = f"SELECT {col_list} FROM health_events {where} ORDER BY id DESC LIMIT ?"
        rows = _conn.execute(query, (*params, limit)).fetchall()
        rows = sorted(rows, key=lambda r: r[0])
    else:
        query = f"SELECT {col_list} FROM health_events {where} ORDER BY id"
        rows = _conn.execute(query, params).fetchall()

    return [dict(zip(cols[1:], row[1:])) for row in rows]


def recent_telemetry(topic, since, limit):
    """Telemetry rows for one topic, recorded (wall-clock) at or after
    `since` (unix seconds), most recent first, capped at `limit`. Pooled
    across ALL sessions (Option A - no session_id filter) - used to warm
    up a fresh HealthMonitor's statistical baseline on connect, not to
    browse one connection's history. Filtering by wall-clock recency
    means data from a long-past session (robot idle/offline since) is
    naturally excluded once `since` is older than the last write - no
    separate staleness check needed.

    Returns [(recorded_at, data_dict), ...] with `data` already
    JSON-decoded. Deliberately returns `recorded_at` (wall-clock), NOT
    the stored `t` column - `t` is stream-relative (bag: time since bag
    start, live: time since that connection opened) and restarts near 0
    every session, so it's meaningless compared across sessions. The
    HealthMonitor's rolling window needs one consistent clock across
    warm-up and live ingestion - see main.py, which passes time.time()
    into health.ingest() for the same reason."""
    rows = _conn.execute(
        "SELECT recorded_at, data FROM telemetry WHERE topic = ? AND recorded_at >= ? "
        "ORDER BY recorded_at DESC LIMIT ?",
        (topic, since, limit),
    ).fetchall()
    return [(recorded_at, json.loads(data)) for recorded_at, data in rows]


def latest_session_id():
    """The most recent session_id to have logged anything, or None if the
    DB is empty. Used as the default when no session_id is requested."""
    row = _conn.execute(
        "SELECT session_id FROM health_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row:
        return row[0]
    row = _conn.execute(
        "SELECT session_id FROM telemetry ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def list_sessions(limit=50):
    """Distinct session_ids that have logged health events, most recent
    first, each with its first/last event time - lets a caller browse
    past sessions instead of only ever seeing the latest one."""
    rows = _conn.execute(
        """
        SELECT session_id, MIN(logged_at), MAX(logged_at), COUNT(*)
        FROM health_events
        GROUP BY session_id
        ORDER BY MAX(logged_at) DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {"session_id": r[0], "started_at": r[1], "ended_at": r[2], "event_count": r[3]}
        for r in rows
    ]

