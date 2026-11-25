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
import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

# PGUARD_DB_PATH lets tests (and multiple deployments) use their own file.
DB_PATH = Path(os.environ.get("PGUARD_DB_PATH") or (Path(__file__).parent / "dashboard.db"))

def _init_schema(conn):
    """Create every table/index if missing (idempotent). Also used by the
    login tests to build a throwaway database."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_telemetry_topic ON telemetry(topic)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_telemetry_session ON telemetry(session_id)"
    )
    conn.execute(
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_health_events_session ON health_events(session_id)"
    )
    # Security configuration - edited from the dashboard's Settings page, never
    # hardcoded. Passwords are stored only as salted PBKDF2 hashes (auth.py).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            username    TEXT PRIMARY KEY,
            pw_hash     TEXT NOT NULL,   -- hex PBKDF2-HMAC-SHA256
            salt        TEXT NOT NULL,   -- hex, 16 random bytes per user
            iterations  INTEGER NOT NULL,
            created_at  REAL NOT NULL,
            created_by  TEXT,
            role        TEXT NOT NULL DEFAULT 'operator'   -- 'admin' | 'operator'
        )
        """
    )
    # Migration for databases created before roles existed: every account
    # then had full rights, so existing accounts become admins (nobody
    # silently loses access); new accounts default to operator.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
    if "role" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'operator'")
        conn.execute("UPDATE users SET role = 'admin'")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dashboard_allowed_ips (
            entry     TEXT PRIMARY KEY,   -- single IP or CIDR network, normalized
            label     TEXT,
            added_at  REAL NOT NULL,
            added_by  TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trusted_ips (
            ip        TEXT PRIMARY KEY,
            label     TEXT,
            added_at  REAL NOT NULL,
            added_by  TEXT
        )
        """
    )
    # Audit trail of security-relevant actions (logins, lockouts, blocked
    # computers, settings changes, alert acknowledgements). Append-only from
    # the app (no API edits or deletes it) and hash-chained: each row stores
    # sha256(previous row_hash + this row's fields), so editing or deleting
    # any past row breaks every hash after it - see audit_verify().
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            at        REAL NOT NULL,     -- wall-clock unix seconds
            event     TEXT NOT NULL,     -- e.g. login_ok, login_failed, alert_ack
            level     TEXT NOT NULL,     -- 'info' or 'warning'
            actor     TEXT,              -- username (None = not signed in)
            ip        TEXT,
            detail    TEXT,
            prev_hash TEXT NOT NULL,
            row_hash  TEXT NOT NULL
        )
        """
    )
    # Operator acknowledgements of active alerts - shared by every dashboard,
    # kept across reloads. An ack is "open" until the alert clears, the
    # operator undoes it, or the backend restarts (fresh monitors re-detect
    # everything, so old acks must not silently hide a new occurrence).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_acks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_key    TEXT NOT NULL,
            kind         TEXT,
            severity     TEXT,
            signal       TEXT,
            message      TEXT,
            acked_by     TEXT,
            acked_ip     TEXT,
            acked_at     REAL NOT NULL,
            resolved_at  REAL,           -- NULL = still open
            resolved_how TEXT,           -- 'cleared' | 'alert_gone' | 'undone' | 'restart'
            resolved_by  TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_acks_open ON alert_acks(alert_key, resolved_at)")
    # Tunable policy + robot address, edited from Settings (see policy.py for
    # the allowed keys, defaults and safe bounds). A key with no row = default.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,     -- JSON-encoded
            updated_at REAL NOT NULL,
            updated_by TEXT
        )
        """
    )
    conn.commit()


_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_init_schema(_conn)


# ---------- users (settings writes commit immediately, not batched) ----------
def count_users():
    return _conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def get_user(username):
    row = _conn.execute(
        "SELECT username, pw_hash, salt, iterations, role FROM users WHERE username = ?",
        (username,)).fetchone()
    if row is None:
        return None
    return {"username": row[0], "pw_hash": row[1], "salt": row[2], "iterations": row[3], "role": row[4]}


def list_users():
    rows = _conn.execute(
        "SELECT username, created_at, created_by, role FROM users ORDER BY username").fetchall()
    return [{"username": r[0], "created_at": r[1], "created_by": r[2], "role": r[3]} for r in rows]


def add_user(username, pw_hash, salt, iterations, created_by=None, role="operator"):
    _conn.execute(
        "INSERT INTO users (username, pw_hash, salt, iterations, created_at, created_by, role) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (username, pw_hash, salt, iterations, time.time(), created_by, role))
    _conn.commit()


def count_admins():
    return _conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]


def set_user_role(username, role):
    cur = _conn.execute("UPDATE users SET role = ? WHERE username = ?", (role, username))
    _conn.commit()
    return cur.rowcount == 1


def set_user_password(username, pw_hash, salt, iterations):
    cur = _conn.execute(
        "UPDATE users SET pw_hash = ?, salt = ?, iterations = ? WHERE username = ?",
        (pw_hash, salt, iterations, username))
    _conn.commit()
    return cur.rowcount == 1


def delete_user(username):
    cur = _conn.execute("DELETE FROM users WHERE username = ?", (username,))
    _conn.commit()
    return cur.rowcount == 1


# ---------- dashboard access allowlist (who may open the dashboard) ----------
def list_dashboard_ips():
    rows = _conn.execute(
        "SELECT entry, label, added_at, added_by FROM dashboard_allowed_ips ORDER BY entry").fetchall()
    return [{"entry": r[0], "label": r[1], "added_at": r[2], "added_by": r[3]} for r in rows]


def dashboard_ip_entries():
    return [r[0] for r in _conn.execute("SELECT entry FROM dashboard_allowed_ips").fetchall()]


def add_dashboard_ip(entry, label=None, added_by=None):
    _conn.execute(
        "INSERT OR REPLACE INTO dashboard_allowed_ips (entry, label, added_at, added_by) VALUES (?, ?, ?, ?)",
        (entry, label, time.time(), added_by))
    _conn.commit()


def remove_dashboard_ip(entry):
    cur = _conn.execute("DELETE FROM dashboard_allowed_ips WHERE entry = ?", (entry,))
    _conn.commit()
    return cur.rowcount == 1


# ---------- trusted IPs (Security watch allowlist) ----------
def list_trusted_ips():
    rows = _conn.execute(
        "SELECT ip, label, added_at, added_by FROM trusted_ips ORDER BY ip").fetchall()
    return [{"ip": r[0], "label": r[1], "added_at": r[2], "added_by": r[3]} for r in rows]


def trusted_ip_set():
    return {r[0] for r in _conn.execute("SELECT ip FROM trusted_ips").fetchall()}


def add_trusted_ip(ip, label=None, added_by=None):
    _conn.execute(
        "INSERT OR REPLACE INTO trusted_ips (ip, label, added_at, added_by) VALUES (?, ?, ?, ?)",
        (ip, label, time.time(), added_by))
    _conn.commit()


def remove_trusted_ip(ip):
    cur = _conn.execute("DELETE FROM trusted_ips WHERE ip = ?", (ip,))
    _conn.commit()
    return cur.rowcount == 1


# ---------- settings (key/value; validation lives in policy.py) ----------
def settings_all():
    rows = _conn.execute("SELECT key, value, updated_at, updated_by FROM settings").fetchall()
    return {k: {"value": json.loads(v), "updated_at": at, "updated_by": by} for k, v, at, by in rows}


def setting_put(key, value, by=None):
    _conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
                  (key, json.dumps(value), time.time(), by))
    _conn.commit()


def setting_delete(key):
    cur = _conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    _conn.commit()
    return cur.rowcount == 1


# ---------- audit log (hash-chained, append-only) ----------
_audit_lock = threading.Lock()
AUDIT_GENESIS = "0" * 64


def _audit_hash(prev_hash, at, event, level, actor, ip, detail):
    payload = "|".join([prev_hash, f"{at:.6f}", event, level, actor or "", ip or "", detail or ""])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def audit_append(event, actor=None, ip=None, detail=None, level="info"):
    """Append one audit row and commit at once (a security record must not
    sit in the write batch). Never raises: auditing must not break login."""
    try:
        with _audit_lock:
            row = _conn.execute("SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
            prev = row[0] if row else AUDIT_GENESIS
            at = time.time()
            h = _audit_hash(prev, at, event, level, actor, ip, detail)
            _conn.execute(
                "INSERT INTO audit_log (at, event, level, actor, ip, detail, prev_hash, row_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (at, event, level, actor, ip, detail, prev, h))
            _conn.commit()
    except Exception:
        pass


def audit_list(limit=200, level=None):
    """Most recent first."""
    where, params = "", []
    if level:
        where, params = "WHERE level = ?", [level]
    rows = _conn.execute(
        f"SELECT id, at, event, level, actor, ip, detail FROM audit_log {where} ORDER BY id DESC LIMIT ?",
        (*params, limit)).fetchall()
    cols = ("id", "at", "event", "level", "actor", "ip", "detail")
    return [dict(zip(cols, r)) for r in rows]


def audit_verify():
    """Recompute the whole chain. Returns {ok, count, broken_at, reason}:
    broken_at is the first row id whose stored hash no longer matches
    (edited row) or whose prev_hash doesn't link (deleted/inserted row)."""
    prev = AUDIT_GENESIS
    count = 0
    last_id = 0
    for rid, at, event, level, actor, ip, detail, prev_hash, row_hash in _conn.execute(
            "SELECT id, at, event, level, actor, ip, detail, prev_hash, row_hash FROM audit_log ORDER BY id"):
        if prev_hash != prev:
            return {"ok": False, "count": count, "broken_at": rid,
                    "reason": f"row {rid} does not follow row {last_id} (a row was deleted or inserted)"}
        if _audit_hash(prev_hash, at, event, level, actor, ip, detail) != row_hash:
            return {"ok": False, "count": count, "broken_at": rid,
                    "reason": f"row {rid} was modified after it was written"}
        prev, last_id, count = row_hash, rid, count + 1
    return {"ok": True, "count": count, "broken_at": None, "reason": None}


# ---------- alert acknowledgements ----------
_ACK_COLS = ("id", "alert_key", "kind", "severity", "signal", "message",
             "acked_by", "acked_ip", "acked_at", "resolved_at", "resolved_how", "resolved_by")


def acks_open():
    rows = _conn.execute(
        f"SELECT {', '.join(_ACK_COLS)} FROM alert_acks WHERE resolved_at IS NULL ORDER BY acked_at").fetchall()
    return [dict(zip(_ACK_COLS, r)) for r in rows]


def acks_recent(limit=50):
    rows = _conn.execute(
        f"SELECT {', '.join(_ACK_COLS)} FROM alert_acks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(zip(_ACK_COLS, r)) for r in rows]


def ack_open(key, info, user, ip):
    """Open an ack for `key`. False if one is already open (someone else
    acknowledged it first - theirs stands)."""
    if _conn.execute("SELECT 1 FROM alert_acks WHERE alert_key = ? AND resolved_at IS NULL",
                     (key,)).fetchone():
        return False
    _conn.execute(
        "INSERT INTO alert_acks (alert_key, kind, severity, signal, message, acked_by, acked_ip, acked_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (key, info.get("kind"), info.get("severity"), info.get("signal"), info.get("message"),
         user, ip, time.time()))
    _conn.commit()
    return True


def ack_resolve(key, how, by=None):
    cur = _conn.execute(
        "UPDATE alert_acks SET resolved_at = ?, resolved_how = ?, resolved_by = ? "
        "WHERE alert_key = ? AND resolved_at IS NULL", (time.time(), how, by, key))
    _conn.commit()
    return cur.rowcount


def acks_resolve_all(how):
    cur = _conn.execute(
        "UPDATE alert_acks SET resolved_at = ?, resolved_how = ? WHERE resolved_at IS NULL",
        (time.time(), how))
    _conn.commit()
    return cur.rowcount


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

