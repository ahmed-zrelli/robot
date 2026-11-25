"""Dashboard login - operator accounts stored in SQLite, server-side sessions.

Accounts and the Security-watch trusted-IP list live in the database (see
db.py `users` / `trusted_ips`) and are managed from the dashboard's
Settings page - nothing security-relevant is hardcoded.

- Login is enabled as soon as at least one account exists. With no
  account the dashboard is open (first-run / dev) and the Settings page
  lets you create the first one.
- One-time migration: if the users table is empty and DASHBOARD_PASSWORD
  is set, an "admin" account is created from it at startup. After that the
  variable is ignored - change the password from the Settings page.
- Passwords: PBKDF2-HMAC-SHA256, 600 000 iterations (OWASP 2023 guidance
  for PBKDF2-SHA256), 16-byte random salt per user; only the hash is
  stored. Verification uses hmac.compare_digest (constant time), and an
  unknown username still costs one full hash so response time does not
  reveal which usernames exist.
- Policy for passwords set through the app: at least 8 characters and
  different from the username. A weak migrated password is allowed to log
  in but the Settings page asks to change it.
- Everything is protected, not just the page: every HTTP route goes
  through `require_login`, and the telemetry WebSocket checks the same
  session before streaming.
- Session = random 256-bit token kept server-side (user, expiry); the
  browser only holds it in an HttpOnly, SameSite=Strict cookie. Deleting a
  user or changing a password ends that user's other sessions.
- Brute-force brake: N wrong logins from one IP within a window lock
  that IP out for a while (N, window, lockout, session length and minimum
  password length are policy values in the DB - see policy.py; Settings
  can tighten them but never go below the safe floor). Every login, failure,
  lockout, logout and settings change is logged.
- Sessions live in memory: restarting the backend logs everyone out.
"""
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import time
from urllib.parse import parse_qs

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

import db
import policy

log = logging.getLogger("auth")

COOKIE = "pguard_session"
PBKDF2_ITERATIONS = 600_000      # fixed in code on purpose: never lowered from a web page
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
# Reachable without a session: the login page and the logo it shows.
PUBLIC_PATHS = {"/login", "/assets/enova.png", "/favicon.ico", "/api/auth-status"}

_sessions = {}        # token -> {"user", "exp", "weak"}
_fails = {}           # ip -> [timestamps of recent failures]
_locked_until = {}    # ip -> epoch s
_DUMMY_SALT = secrets.token_bytes(16)
_blocked_audit_at = {}  # ip -> last time a "blocked" row was written (throttle)
BLOCKED_AUDIT_EVERY_S = 60


def audit(event, ip=None, actor=None, detail=None, level="info"):
    """One security event -> server log AND the hash-chained audit_log
    table (Settings -> Audit log). The DB copy survives restarts."""
    (log.warning if level == "warning" else log.info)(
        "[audit] %s actor=%s ip=%s %s", event, actor or "-", ip or "-", detail or "")
    db.audit_append(event, actor=actor, ip=ip, detail=detail, level=level)


# ---------- password hashing ----------
def hash_password(password, salt=None, iterations=None):
    salt = salt if salt is not None else secrets.token_bytes(16)
    iterations = iterations or PBKDF2_ITERATIONS
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return digest.hex(), salt.hex(), iterations


def verify_password(username, password):
    user = db.get_user(username)
    if user is None:
        hash_password(password, _DUMMY_SALT)  # same cost as a real check
        return False
    digest, _, _ = hash_password(password, bytes.fromhex(user["salt"]), user["iterations"])
    return hmac.compare_digest(digest, user["pw_hash"])


def password_problem(username, password):
    min_len = policy.get("password_min_length")
    if len(password) < min_len:
        return f"Password must be at least {min_len} characters."
    if password.lower() == username.lower():
        return "Password must be different from the username."
    return None


def create_user(username, password, created_by=None, enforce_policy=True):
    if not USERNAME_RE.match(username or ""):
        return "Username: 3-32 letters, digits, '.', '_' or '-'."
    if db.get_user(username) is not None:
        return "This username already exists."
    if enforce_policy:
        problem = password_problem(username, password)
        if problem:
            return problem
    db.add_user(username, *hash_password(password), created_by=created_by)
    return None


def bootstrap_from_env():
    """One-time migration from the old env-var password (see module doc)."""
    pw = os.environ.get("DASHBOARD_PASSWORD")
    if db.count_users() == 0 and pw:
        create_user("admin", pw, created_by="DASHBOARD_PASSWORD (migration)", enforce_policy=False)
        audit("user_created", actor="DASHBOARD_PASSWORD (migration)",
              detail="account 'admin' created from DASHBOARD_PASSWORD; manage accounts from Settings",
              level="warning")


# ---------- sessions ----------
def enabled():
    return db.count_users() > 0


def _session(token):
    s = _sessions.get(token or "")
    if s is None:
        return None
    if s["exp"] < time.time():
        _sessions.pop(token, None)
        return None
    return s


def valid_token(token):
    return True if not enabled() else _session(token) is not None


def current_user(request):
    """Username of the request's session; None when login is disabled."""
    s = _session(request.cookies.get(COOKIE))
    return s["user"] if s else None


def _end_sessions(username, keep=None):
    for tok in [t for t, s in _sessions.items() if s["user"] == username and t != keep]:
        _sessions.pop(tok, None)


def _client_ip(request):
    return request.client.host if request.client else "?"


# ---------- login / logout ----------
def login_page(error="", status=200, username=""):
    msg = f'<div class="err">{error}</div>' if error else ""
    user_val = username.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>PGuard — Sign in</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0B0F17;color:#E8EDF2;font-family:'Segoe UI',system-ui,sans-serif}}
.card{{width:min(380px,92vw);background:linear-gradient(135deg,#10182A,#162033);border:1px solid #1A2B42;border-top:3px solid #00B4B4;border-radius:14px;padding:30px 28px}}
img{{height:40px;display:block;margin:0 auto 14px}}h1{{font-size:16px;text-align:center;margin:0 0 2px;letter-spacing:.5px}}
.sub{{text-align:center;color:#6B8299;font-size:11.5px;margin-bottom:22px}}label{{font-size:10px;text-transform:uppercase;letter-spacing:1.2px;color:#6B8299}}
input{{width:100%;margin:6px 0 16px;padding:10px 12px;border-radius:8px;border:1px solid #1A2B42;background:#0B0F17;color:#E8EDF2;font-size:14px}}
input:focus{{outline:2px solid #00B4B4;outline-offset:1px}}
button{{width:100%;padding:10px;border-radius:8px;border:1px solid rgba(0,180,180,.55);background:linear-gradient(135deg,rgba(0,180,180,.35),rgba(0,180,180,.18));color:#1FD1D1;font-weight:700;font-size:13px;cursor:pointer}}
.err{{background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.45);color:#FCA5A5;border-radius:8px;padding:8px 10px;font-size:12px;margin-bottom:14px}}
.foot{{margin-top:16px;text-align:center;font-size:10.5px;color:#3E5168}}
</style></head><body><form class="card" method="post" action="/login">
<img src="/assets/enova.png" alt="Enova Robotics"><h1>PGuard — Robot Health Monitor</h1><div class="sub">Operator access</div>
{msg}<label for="user">Username</label><input id="user" name="username" autocomplete="username" value="{user_val}" {'autofocus' if not user_val else ''} required>
<label for="pw">Password</label><input id="pw" name="password" type="password" autocomplete="current-password" {'autofocus' if user_val else ''} required>
<button type="submit">Sign in</button><div class="foot">Authorized personnel only · access is logged</div></form></body></html>"""
    return HTMLResponse(html, status_code=status)


async def handle_login(request: Request):
    ip = _client_ip(request)
    now = time.time()
    if _locked_until.get(ip, 0) > now:
        wait = int(_locked_until[ip] - now) + 1
        audit("login_refused_locked", ip, detail=f"IP locked out, {wait} s left", level="warning")
        return login_page(f"Too many failed attempts. Try again in {wait} s.", 429)
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    username = (form.get("username") or [""])[0].strip()
    given = (form.get("password") or [""])[0]
    max_fails = policy.get("login_max_fails")
    window_s = policy.get("login_fail_window_s")
    lockout_s = policy.get("login_lockout_s")
    ttl_s = policy.get("session_ttl_h") * 3600
    if enabled() and verify_password(username, given):
        audit("login_ok", ip, username)
        _fails.pop(ip, None)
        token = secrets.token_urlsafe(32)
        _sessions[token] = {"user": username, "exp": now + ttl_s,
                            "weak": password_problem(username, given) is not None}
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, token, max_age=ttl_s, httponly=True,
                        samesite="strict", secure=request.url.scheme == "https", path="/")
        return resp
    recent = [t for t in _fails.get(ip, []) if now - t < window_s] + [now]
    _fails[ip] = recent
    if len(recent) >= max_fails:
        _locked_until[ip] = now + lockout_s
        _fails.pop(ip, None)
        audit("lockout", ip, detail=f"{max_fails} failed logins in {window_s} s (last tried "
              f"username '{username[:40]}') - IP locked for {lockout_s} s", level="warning")
        return login_page(f"Too many failed attempts. Locked for {lockout_s} s.", 429)
    audit("login_failed", ip, detail=f"username '{username[:40]}' ({len(recent)}/{max_fails})", level="warning")
    # Same message for unknown user and wrong password: no username enumeration.
    return login_page(f"Wrong username or password ({max_fails - len(recent)} attempt(s) left).",
                      401, username)


def handle_logout(request: Request):
    s = _sessions.pop(request.cookies.get(COOKIE, ""), None)
    if s is not None:
        audit("logout", _client_ip(request), s["user"])
    resp = RedirectResponse("/login" if enabled() else "/", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


# ---------- dashboard access allowlist (network layer, before login) ----------
# Managed on the Settings page (db.dashboard_allowed_ips). Empty list = every
# IP may reach the login page (previous behaviour). Non-empty = only listed
# IPs / CIDR networks; everyone else gets a bare 404 before the login page, so they
# cannot even try passwords. Two anti-lockout rules:
#  - loopback (this machine) is always allowed - recovery path if the list
#    is ever wrong. Caveat, measured: Docker Desktop relays containers'
#    requests to host.docker.internal through 127.0.0.1, so containers on
#    the SAME PC count as local. Other machines keep their real IPs.
#  - the Settings API refuses a change that would exclude the caller.
# The client IP is the TCP peer (request.client.host). X-Forwarded-For is
# deliberately ignored: with no reverse proxy in front, any client could
# forge it. Behind a proxy, configure uvicorn --forwarded-allow-ips instead.
def parse_allow_entry(raw):
    """'10.0.0.5' or '192.168.1.0/24' -> normalized string, or None."""
    try:
        return str(ipaddress.ip_network(raw.strip(), strict=False))
    except ValueError:
        return None


def ip_allowed(host, entries=None):
    entries = db.dashboard_ip_entries() if entries is None else entries
    if not entries:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    return any(ip in ipaddress.ip_network(e, strict=False) for e in entries)


def denied_response():
    """What a computer outside the access list sees: a bare, generic 404,
    identical for every path (page, API, assets). No branding, no product
    name, no hint that a dashboard or a login exists here - a blocked
    scanner learns nothing it could use. The real reason is only in the
    server log. (Truly not answering at all needs an OS firewall rule; see
    README "Security".)"""
    return PlainTextResponse("Not Found", status_code=404)


def note_blocked(ip, what):
    """A computer outside the access list knocked. Logged every time in the
    server log, but written to the audit table at most once per IP per
    BLOCKED_AUDIT_EVERY_S - a scanner must not be able to flood the DB."""
    log.warning("[auth] blocked %s from %s: not in dashboard allowed IPs (answered bare 404)", what, ip)
    now = time.time()
    if now - _blocked_audit_at.get(ip, 0) >= BLOCKED_AUDIT_EVERY_S:
        _blocked_audit_at[ip] = now
        db.audit_append("access_blocked", ip=ip, level="warning",
                        detail=f"{what} - not in dashboard allowed IPs (answered bare 404); "
                               f"further attempts from this IP not recorded for {BLOCKED_AUDIT_EVERY_S} s")


async def require_login(request: Request, call_next):
    """HTTP middleware. WebSockets are checked separately in main.py
    (Starlette HTTP middleware does not see WebSocket handshakes)."""
    ip = _client_ip(request)
    if not ip_allowed(ip):
        note_blocked(ip, f"{request.method} {request.url.path}")
        return denied_response()
    if not enabled() or request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    if valid_token(request.cookies.get(COOKIE)):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": "authentication required"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


# ---------- Settings API (all behind require_login) ----------
def _err(msg, status=400):
    return JSONResponse({"ok": False, "error": msg}, status_code=status)


async def _json(request):
    # Requiring a JSON body means a cross-site HTML form cannot forge these
    # requests (browsers can't send application/json cross-site without a
    # CORS preflight this server never grants); SameSite=Strict covers the rest.
    if not request.headers.get("content-type", "").startswith("application/json"):
        return None
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def settings_state(request):
    s = _session(request.cookies.get(COOKIE))
    return {
        "ok": True,
        "auth_enabled": enabled(),
        "me": s["user"] if s else None,
        "weak_password": bool(s and s.get("weak")),
        "password_policy": {"min_length": policy.get("password_min_length")},
        "users": db.list_users(),
        "trusted_ips": db.list_trusted_ips(),
        "dashboard_ips": db.list_dashboard_ips(),
        "your_ip": _client_ip(request),
        "trusted_ips_env": [ip.strip() for ip in os.environ.get("SECURITY_TRUSTED_IPS", "").split(",") if ip.strip()],
    }


async def change_password(request):
    me = current_user(request)
    body = await _json(request)
    if me is None:
        return _err("Sign in first.", 401)
    if body is None:
        return _err("Expected a JSON body.")
    if not verify_password(me, str(body.get("current", ""))):
        audit("password_change_refused", _client_ip(request), me, "wrong current password", "warning")
        return _err("Current password is wrong.", 403)
    new = str(body.get("new", ""))
    problem = password_problem(me, new)
    if problem:
        return _err(problem)
    db.set_user_password(me, *hash_password(new))
    keep = request.cookies.get(COOKIE)
    _end_sessions(me, keep=keep)
    if keep in _sessions:
        _sessions[keep]["weak"] = False
    audit("password_changed", _client_ip(request), me)
    return {"ok": True}


async def add_user(request):
    body = await _json(request)
    if body is None:
        return _err("Expected a JSON body.")
    actor = current_user(request) or "first-run setup"
    problem = create_user(str(body.get("username", "")).strip(), str(body.get("password", "")),
                          created_by=actor)
    if problem:
        return _err(problem)
    audit("user_created", _client_ip(request), actor, f"account '{str(body.get('username')).strip()}'")
    return {"ok": True}


def delete_user(request, username):
    me = current_user(request)
    if username == me:
        return _err("You cannot delete your own account.")
    if db.get_user(username) is None:
        return _err("No such user.", 404)
    if db.count_users() <= 1:
        return _err("Cannot delete the last account.")
    db.delete_user(username)
    _end_sessions(username)
    audit("user_deleted", _client_ip(request), me, f"account '{username}'", "warning")
    return {"ok": True}


async def add_trusted_ip(request):
    body = await _json(request)
    if body is None:
        return _err("Expected a JSON body.")
    raw = str(body.get("ip", "")).strip()
    try:
        ip = str(ipaddress.ip_address(raw))
    except ValueError:
        return _err("Not a valid IP address (e.g. 172.28.0.1).")
    label = str(body.get("label", "")).strip()[:60] or None
    db.add_trusted_ip(ip, label, added_by=current_user(request))
    audit("trusted_ip_added", _client_ip(request), current_user(request), f"{ip} ({label or 'no label'})")
    return {"ok": True}


async def add_dashboard_ip(request):
    body = await _json(request)
    if body is None:
        return _err("Expected a JSON body.")
    entry = parse_allow_entry(str(body.get("entry", "")))
    if entry is None:
        return _err("Not a valid IP or network (e.g. 192.168.1.20 or 192.168.1.0/24).")
    me_ip = _client_ip(request)
    after = set(db.dashboard_ip_entries()) | {entry}
    if not ip_allowed(me_ip, list(after)):
        return _err(f"Refused: this list would lock out your own computer ({me_ip}). Add it first.")
    label = str(body.get("label", "")).strip()[:60] or None
    db.add_dashboard_ip(entry, label, added_by=current_user(request))
    audit("dashboard_ip_added", _client_ip(request), current_user(request), f"{entry} ({label or 'no label'})")
    return {"ok": True}


def remove_dashboard_ip(request, entry):
    entry = parse_allow_entry(entry) or entry
    after = [e for e in db.dashboard_ip_entries() if e != entry]
    me_ip = _client_ip(request)
    if after and not ip_allowed(me_ip, after):
        return _err(f"Refused: removing it would lock out your own computer ({me_ip}).")
    if not db.remove_dashboard_ip(entry):
        return _err("Not in the list.", 404)
    audit("dashboard_ip_removed", _client_ip(request), current_user(request), entry, "warning")
    return {"ok": True}


def remove_trusted_ip(request, ip):
    if not db.remove_trusted_ip(ip):
        return _err("Not in the list.", 404)
    audit("trusted_ip_removed", _client_ip(request), current_user(request), ip, "warning")
    return {"ok": True}


# ---------- alert acknowledgements (shared, stored in the DB) ----------
MAX_ACK_FIELD = 300


async def ack_alert(request):
    body = await _json(request)
    if body is None:
        return _err("Expected a JSON body.")
    key = str(body.get("key", "")).strip()
    if not key or len(key) > MAX_ACK_FIELD:
        return _err("Missing or invalid alert key.")
    info = {f: (str(body[f])[:MAX_ACK_FIELD] if body.get(f) is not None else None)
            for f in ("kind", "severity", "signal", "message")}
    me, ip = current_user(request), _client_ip(request)
    if not db.ack_open(key, info, me, ip):
        return _err("Already acknowledged.", 409)
    audit("alert_ack", ip, me, f"{info['severity'] or '?'} {info['signal'] or key}")
    return {"ok": True}


def unack_alert(request, key):
    me, ip = current_user(request), _client_ip(request)
    if not db.ack_resolve(key, "undone", by=me):
        return _err("Not acknowledged.", 404)
    audit("alert_unack", ip, me, key, "warning")
    return {"ok": True}


# Which alerts are active on which open dashboard connection (each
# connection runs its own HealthMonitor on the same robot). An ack stays
# open while at least one connection still shows the alert, and closes
# when the last one clears it or disconnects - so a later recurrence is
# never shown as "already acknowledged".
_active_alerts = {}   # session_id -> set of alert keys


def _close_acks_if_gone(keys, how):
    for k in keys:
        if not any(k in s for s in _active_alerts.values()):
            db.ack_resolve(k, how)


def note_alert(session_id, ev):
    key = ev.get("key")
    if not key:
        return
    keys = _active_alerts.setdefault(session_id, set())
    if ev.get("state") == "new":
        keys.add(key)
    elif ev.get("state") == "cleared":
        keys.discard(key)
        _close_acks_if_gone([key], "cleared")


# Must exceed the slowest re-detection on a fresh connection: a frozen
# (dead) sensor needs 90 s of data - measured ~75 s after a reload on the
# virtual robot. 150 s = 2x margin.
ACK_GRACE_S = 150.0


def end_alert_session(session_id, grace_s=ACK_GRACE_S):
    """A dashboard connection ended. Its alerts' acks close only if, after a
    grace period, no connection shows them again - so reloading the page
    keeps the operator's acks, but an alert that is really gone doesn't
    leave a stale ack that would hide its next occurrence."""
    keys = _active_alerts.pop(session_id, set())
    if not keys:
        return
    if grace_s <= 0:
        _close_acks_if_gone(keys, "alert_gone")
        return
    import asyncio
    asyncio.get_running_loop().call_later(grace_s, _close_acks_if_gone, keys, "alert_gone")


def acks_state():
    return {"ok": True, "open": db.acks_open()}


# ---------- audit log (read-only from the app) ----------
def audit_state(request):
    try:
        limit = max(1, min(1000, int(request.query_params.get("limit", 200))))
    except ValueError:
        limit = 200
    level = request.query_params.get("level") or None
    if level not in (None, "info", "warning"):
        level = None
    return {"ok": True, "entries": db.audit_list(limit, level), "integrity": db.audit_verify()}


# ---------- policy + robot address (Settings -> Policy / Robot connection) ----------
def policy_state():
    return {"ok": True, "policy": policy.describe(), "robot": policy.robot()}


async def update_policy(request):
    """Body: {key: value, ...}. All-or-nothing: one invalid value refuses
    the whole change, so a half-applied policy never exists."""
    body = await _json(request)
    if not body:
        return _err("Expected a JSON body.")
    clean = {}
    for key, raw in body.items():
        value, problem = policy.validate(key, raw)
        if problem:
            return _err(problem)
        clean[key] = value
    me, ip = current_user(request), _client_ip(request)
    changed = []
    for key, value in clean.items():
        old = policy.get(key)
        if value == old:
            continue
        db.setting_put(key, value, by=me)
        changed.append(f"{key} {old:g} -> {value:g}")
    policy.invalidate()
    if changed:
        audit("policy_changed", ip, me, "; ".join(changed), "warning")
    return {"ok": True, "changed": len(changed)}


def reset_policy(request, key):
    if key not in policy.POLICY:
        return _err("Unknown setting.", 404)
    old = policy.get(key)
    if db.setting_delete(key):
        policy.invalidate()
        audit("policy_changed", _client_ip(request), current_user(request),
              f"{key} {old:g} -> default {policy.get(key):g}", "warning")
    return {"ok": True}


async def parse_robot_body(request):
    """-> (host, port, speedtest_port, None) or (None, None, None, error)."""
    body = await _json(request)
    if body is None:
        return None, None, None, "Expected a JSON body."
    host = policy.valid_host(str(body.get("host", "")))
    if host is None:
        return None, None, None, "Robot address: an IP address or host name (e.g. 192.168.1.50 or pguard.local)."
    ports = []
    for field, label in (("port", "rosbridge port"), ("speedtest_port", "speed-test port")):
        try:
            v = int(body.get(field))
            if isinstance(body.get(field), bool) or not 1 <= v <= 65535 or float(body.get(field)) != v:
                raise ValueError
        except (TypeError, ValueError):
            return None, None, None, f"{label}: a whole number between 1 and 65535."
        ports.append(v)
    return host, ports[0], ports[1], None


async def update_robot(request):
    host, port, st_port, problem = await parse_robot_body(request)
    if problem:
        return _err(problem)
    old = policy.robot()
    me = current_user(request)
    db.setting_put("robot_host", host, by=me)
    db.setting_put("robot_port", port, by=me)
    db.setting_put("speedtest_port", st_port, by=me)
    policy.invalidate()
    audit("robot_address_changed", _client_ip(request), me,
          f"{old['host']}:{old['port']} -> {host}:{port} (speed test port {st_port})", "warning")
    return {"ok": True, "robot": policy.robot()}


def reset_robot(request):
    old = policy.robot()
    for k in policy.ROBOT_KEYS:
        db.setting_delete(k)
    policy.invalidate()
    new = policy.robot()
    audit("robot_address_changed", _client_ip(request), current_user(request),
          f"{old['host']}:{old['port']} -> {new['host']}:{new['port']} ({new['source']})", "warning")
    return {"ok": True, "robot": new}
