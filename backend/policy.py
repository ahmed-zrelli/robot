"""Tunable security policy and robot address - stored in the DB `settings`
table and edited from the Settings page, not hardcoded.

Every key has a default (the value that was validated during the
internship) and hard bounds. The bounds are the security floor: the page
can tighten a policy freely but can never weaken it below a safe minimum
(e.g. no 3-character passwords, no lockout shorter than 30 s). Anything
outside the bounds is refused with a message, never clamped silently.

Reads are cached in memory and the cache is dropped on every write, so the
health monitor can ask for the motor-command limit on every message
without hitting SQLite.
"""
import ipaddress
import os
import re

import db

# key: (default, min, max, kind, label, unit, help)
POLICY = {
    "login_max_fails":     (5,   3,   20,   int,   "Failed logins before lockout", "attempts",
                            "Counted per IP address."),
    "login_fail_window_s": (300, 60,  3600, int,   "Counting window for failed logins", "s",
                            "Failures older than this are forgotten."),
    "login_lockout_s":     (60,  30,  3600, int,   "Lockout duration", "s",
                            "Even the right password is refused during the lockout."),
    "session_ttl_h":       (8,   1,   24,   int,   "Session length", "h",
                            "Operators are signed out after this long (one shift by default)."),
    "password_min_length": (8,   8,   64,   int,   "Minimum password length", "characters",
                            "Applies to new passwords; 8 is the floor (OWASP)."),
    "security_max_cmd_rad_s": (240.0, 200.0, 600.0, float, "Security watch: max motor command", "rad/s",
                               "Above this a command is flagged as injected. The reference recording "
                               "never exceeds 233 rad/s measured / 212 rad/s commanded."),
    "security_cmd_hold_s": (60,  10,  600,  int,   "Security watch: forged-command alert hold", "s",
                            "The alert stays up this long after the last out-of-range command."),
}

ROBOT_KEYS = ("robot_host", "robot_port", "speedtest_port")
HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")

_cache = None


def _rows():
    global _cache
    if _cache is None:
        _cache = db.settings_all()
    return _cache


def invalidate():
    global _cache
    _cache = None


def get(key):
    row = _rows().get(key)
    default, _lo, _hi, kind, *_ = POLICY[key]
    return kind(row["value"]) if row else default


def validate(key, raw):
    """-> (value, None) or (None, error message)."""
    if key not in POLICY:
        return None, f"Unknown setting '{key}'."
    default, lo, hi, kind, label, unit, _ = POLICY[key]
    try:
        if isinstance(raw, bool):
            raise ValueError
        value = kind(raw)
        if kind is int and float(raw) != value:
            raise ValueError
    except (TypeError, ValueError):
        return None, f"{label}: expected a {'whole ' if kind is int else ''}number."
    if value != value or not (lo <= value <= hi):  # NaN check + bounds
        return None, f"{label}: must be between {lo:g} and {hi:g} {unit}."
    return value, None


def describe():
    rows = _rows()
    out = []
    for key, (default, lo, hi, kind, label, unit, help_) in POLICY.items():
        row = rows.get(key)
        out.append({"key": key, "label": label, "unit": unit, "help": help_, "default": default,
                    "min": lo, "max": hi, "integer": kind is int, "value": get(key),
                    "updated_at": row["updated_at"] if row else None,
                    "updated_by": row["updated_by"] if row else None})
    return out


# ---------- robot address ----------
def valid_host(raw):
    raw = (raw or "").strip()
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        pass
    return raw.lower() if HOSTNAME_RE.match(raw) else None


def robot():
    """Effective robot address. Priority: Settings (DB) -> env -> default.
    `source` says which, so the page can show where a value comes from."""
    rows = _rows()
    env_host = os.environ.get("ROSBRIDGE_HOST")
    if "robot_host" in rows:
        host, source = rows["robot_host"]["value"], "settings"
    elif env_host:
        host, source = env_host, "env"
    else:
        host, source = "localhost", "default"
    port = int(rows["robot_port"]["value"]) if "robot_port" in rows else int(os.environ.get("ROSBRIDGE_PORT", "9090"))
    st_port = (int(rows["speedtest_port"]["value"]) if "speedtest_port" in rows
               else int(os.environ.get("SPEEDTEST_PORT", "8091")))
    # The speed test follows the robot unless SPEEDTEST_HOST pins it elsewhere.
    st_host = os.environ.get("SPEEDTEST_HOST") or host
    updated = rows.get("robot_host") or rows.get("robot_port")
    return {"host": host, "port": port, "speedtest_host": st_host, "speedtest_port": st_port,
            "source": source,
            "updated_at": updated["updated_at"] if updated else None,
            "updated_by": updated["updated_by"] if updated else None}


def security_limits():
    """For HealthMonitor(security_limits_provider=...)."""
    return get("security_max_cmd_rad_s"), get("security_cmd_hold_s")
