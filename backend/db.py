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
import atexit
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


# Write batching: the streaming loops call record_telemetry/log_event
# per message (hundreds/sec at high replay speed or on a busy robot).
# Committing per row is a synchronous fsync-ish stall on the server's event
# loop each time. Batch instead: commit at most every COMMIT_EVERY_N writes
# or COMMIT_EVERY_S seconds, whichever comes first. Same-connection reads
# (recent_telemetry, read_events, list_sessions) still see uncommitted rows,
# so nothing downstream observes a difference. atexit flush covers the tail
# on clean shutdown; unclean kill can lose <0.5s of trailing rows - an
# acceptable monitoring-dashboard tradeoff, documented here, not silent.
COMMIT_EVERY_N = 200
COMMIT_EVERY_S = 0.5
_pending_writes = 0
_last_commit_t = time.monotonic()


def _maybe_commit():
    global _pending_writes, _last_commit_t
    _pending_writes += 1
    now = time.monotonic()
    if _pending_writes >= COMMIT_EVERY_N or now - _last_commit_t >= COMMIT_EVERY_S:
        _conn.commit()
        _pending_writes = 0
        _last_commit_t = now


def _flush():
    try:
        _conn.commit()
    except Exception:
        pass


atexit.register(_flush)


def record_telemetry(session_id, source, topic, t, data):
    """Append one telemetry message, tagged to its session. Fire-and-
    forget: a DB failure must never take down telemetry streaming."""
    try:
        _conn.execute(
            "INSERT INTO telemetry (session_id, source, topic, t, data, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, source, topic, t, json.dumps(data), time.time()),
        )
        _maybe_commit()
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
        _maybe_commit()
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
    """Distinct session_ids that have logged EITHER a health event OR
    telemetry, most recent first, each with its first/last activity time
    and health-event count - lets a caller browse past sessions instead
    of only ever seeing the latest one.

    Previously grouped by health_events alone, which silently dropped
    any session that streamed telemetry but never triggered a single
    health event (e.g. a short connection, or a quiet stretch of robot
    behavior) - that session existed in the DB and was reachable via
    /api/health-history?session_id=... directly, just invisible from
    this list. latest_session_id() already falls back to telemetry when
    health_events has nothing, so this brings list_sessions in line with
    that same fallback instead of only fixing it in one place.
    event_count reflects health events only (0 for telemetry-only
    sessions) - that field's meaning is unchanged, just no longer used
    to decide which sessions exist."""
    rows = _conn.execute(
        """
        SELECT session_id,
               MIN(activity_at) AS started_at,
               MAX(activity_at) AS ended_at,
               SUM(is_health_event) AS event_count
        FROM (
            SELECT session_id, logged_at AS activity_at, 1 AS is_health_event
            FROM health_events
            UNION ALL
            SELECT session_id, recorded_at AS activity_at, 0 AS is_health_event
            FROM telemetry
        )
        GROUP BY session_id
        ORDER BY ended_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {"session_id": r[0], "started_at": r[1], "ended_at": r[2], "event_count": r[3]}
        for r in rows
    ]


def session_topic_stats(session_id):
    """Per-topic message coverage for one session: message count plus
    stream-relative and wall-clock spans. Read-only aggregate for the
    /api/report endpoint - no new writes, one GROUP BY query."""
    rows = _conn.execute(
        """
        SELECT topic, COUNT(*), MIN(t), MAX(t), MIN(recorded_at),
               MAX(recorded_at)
        FROM telemetry
        WHERE session_id = ?
        GROUP BY topic
        ORDER BY topic
        """,
        (session_id,),
    ).fetchall()
    return [
        {"topic": r[0], "messages": r[1], "first_t": r[2], "last_t": r[3],
         "first_seen": r[4], "last_seen": r[5]}
        for r in rows
    ]

