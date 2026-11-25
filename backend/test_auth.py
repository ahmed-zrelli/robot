"""Dashboard login + Settings API (auth.py, db.py users/trusted_ips) through
the real FastAPI app. Needs fastapi + httpx. Run: python -m unittest test_auth

SAFETY: every test runs on a throwaway SQLite file - db._conn is swapped
in setUp regardless of import order, so the real dashboard.db (and its
accounts) is never read or modified."""
import os
import sqlite3
import tempfile
import unittest

os.environ.setdefault("TELEMETRY_SOURCE", "live")  # never load bag data for these tests
import _testdb  # noqa: E402,F401  (must precede db/main: throwaway DB)

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

import auth  # noqa: E402
import db  # noqa: E402
import main  # noqa: E402
import policy  # noqa: E402

JSON = {"Content-Type": "application/json"}


class Base(unittest.TestCase):
    PASSWORD = "correct horse battery"

    def setUp(self):
        self._real_conn = db._conn
        self._tmp = tempfile.TemporaryDirectory()
        db._conn = sqlite3.connect(os.path.join(self._tmp.name, "t.db"), check_same_thread=False)
        db._init_schema(db._conn)
        self._iters = auth.PBKDF2_ITERATIONS
        auth.PBKDF2_ITERATIONS = 1000  # speed only; the algorithm is unchanged
        auth._sessions.clear(); auth._fails.clear(); auth._locked_until.clear()
        policy.invalidate()  # cache belongs to the previous test's DB
        self.c = TestClient(main.app, follow_redirects=False)

    def tearDown(self):
        db._conn.close()
        db._conn = self._real_conn
        policy.invalidate()
        auth.PBKDF2_ITERATIONS = self._iters
        self._tmp.cleanup()

    def add(self, user="alice", pw=None):
        self.assertIsNone(auth.create_user(user, pw or self.PASSWORD))

    def login(self, user="alice", pw=None, client=None):
        return (client or self.c).post("/login", content=f"username={user}&password={pw or self.PASSWORD}",
                                       headers={"Content-Type": "application/x-www-form-urlencoded"})


class TestStorage(Base):
    def test_password_is_hashed_with_unique_salt(self):
        self.add("alice"); self.add("bob")
        rows = db._conn.execute("SELECT username, pw_hash, salt FROM users").fetchall()
        for _, h, _ in rows:
            self.assertNotIn(self.PASSWORD, h)
            self.assertEqual(len(h), 64)               # sha256 hex digest
        self.assertNotEqual(rows[0][1], rows[1][1])    # same password, different hash...
        self.assertNotEqual(rows[0][2], rows[1][2])    # ...because salts differ

    def test_policy(self):
        self.assertIsNotNone(auth.create_user("carol", "short"))
        self.assertIsNotNone(auth.create_user("carol", "carol"))
        self.assertIsNotNone(auth.create_user("x", self.PASSWORD))      # username rule
        self.assertIsNotNone(auth.create_user("bad name!", self.PASSWORD))

    def test_bootstrap_from_env_once(self):
        os.environ["DASHBOARD_PASSWORD"] = "admin"
        try:
            auth.bootstrap_from_env()
            auth.bootstrap_from_env()
            self.assertEqual([u["username"] for u in db.list_users()], ["admin"])
            self.assertTrue(auth.verify_password("admin", "admin"))
        finally:
            del os.environ["DASHBOARD_PASSWORD"]


class TestNoAccountIsOpen(Base):
    def test_open_then_first_account_turns_login_on(self):
        self.assertEqual(self.c.get("/").status_code, 200)
        r = self.c.post("/api/settings/users", json={"username": "alice", "password": self.PASSWORD})
        self.assertTrue(r.json()["ok"])
        self.assertEqual(self.c.get("/").status_code, 303)  # login now required


class TestLoginRequired(Base):
    def setUp(self):
        super().setUp(); self.add()

    def test_page_api_reports_settings_blocked(self):
        self.assertEqual(self.c.get("/").status_code, 303)
        for path in ("/api/health-history", "/api/report?format=html", "/api/settings", "/settings.html"):
            self.assertIn(self.c.get(path).status_code, (401, 303), path)
        self.assertEqual(self.c.post("/api/settings/trusted-ips", json={"ip": "1.2.3.4"}).status_code, 401)

    def test_websocket_rejected_with_1008(self):
        with self.assertRaises(WebSocketDisconnect) as cm:
            with self.c.websocket_connect("/ws/telemetry") as ws:
                ws.receive_json()
        self.assertEqual(cm.exception.code, 1008)

    def test_login_ok_cookie_hardened(self):
        r = self.login()
        self.assertEqual(r.status_code, 303)
        sc = r.headers["set-cookie"].lower()
        self.assertIn("httponly", sc); self.assertIn("samesite=strict", sc)
        self.assertEqual(self.c.get("/api/auth-status").json()["user"], "alice")

    def test_unknown_user_and_wrong_password_same_answer(self):
        a = self.login("alice", "wrong-password")
        b = self.login("nobody", "wrong-password")
        self.assertEqual((a.status_code, b.status_code), (401, 401))
        self.assertIn("Wrong username or password", a.text); self.assertIn("Wrong username or password", b.text)

    def test_lockout(self):
        for _ in range(policy.get("login_max_fails")):
            self.login(pw="nope-nope")
        self.assertEqual(self.login().status_code, 429)  # right password refused while locked


class TestSettingsApi(Base):
    def setUp(self):
        super().setUp(); self.add("alice"); self.add("bob"); self.login("alice")

    def test_change_password_needs_current_and_ends_other_sessions(self):
        other = TestClient(main.app, follow_redirects=False)
        self.login("alice", client=other)
        self.assertEqual(other.get("/").status_code, 200)
        bad = self.c.post("/api/settings/password", json={"current": "wrong-one", "new": "brand new pass"})
        self.assertEqual(bad.status_code, 403)
        ok = self.c.post("/api/settings/password", json={"current": self.PASSWORD, "new": "brand new pass"})
        self.assertTrue(ok.json()["ok"])
        self.assertEqual(self.c.get("/").status_code, 200)    # this session kept
        self.assertEqual(other.get("/").status_code, 303)     # other session ended
        self.assertTrue(auth.verify_password("alice", "brand new pass"))

    def test_delete_rules(self):
        self.assertFalse(self.c.delete("/api/settings/users/alice").json()["ok"])  # not yourself
        bob = TestClient(main.app, follow_redirects=False)
        self.login("bob", client=bob)
        self.assertTrue(self.c.delete("/api/settings/users/bob").json()["ok"])
        self.assertEqual(bob.get("/").status_code, 303)  # bob's session ended
        self.assertFalse(self.c.delete("/api/settings/users/alice").json()["ok"])  # last account

    def test_trusted_ips_validated_and_stored(self):
        self.assertFalse(self.c.post("/api/settings/trusted-ips", json={"ip": "not-an-ip"}).json()["ok"])
        self.assertTrue(self.c.post("/api/settings/trusted-ips",
                                    json={"ip": "172.28.0.1", "label": "backend"}).json()["ok"])
        self.assertEqual(db.trusted_ip_set(), {"172.28.0.1"})
        self.assertEqual(db.list_trusted_ips()[0]["added_by"], "alice")
        self.assertTrue(self.c.delete("/api/settings/trusted-ips/172.28.0.1").json()["ok"])
        self.assertEqual(db.trusted_ip_set(), set())

    def test_form_post_cannot_change_settings(self):
        # A cross-site HTML form can only send form-encoded bodies: refused.
        r = self.c.post("/api/settings/trusted-ips", content="ip=6.6.6.6",
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertFalse(r.json()["ok"])
        self.assertEqual(db.trusted_ip_set(), set())


class TestDashboardAccessList(Base):
    """Network allowlist in front of the login (Settings → Dashboard access)."""

    def client_from(self, ip):
        return TestClient(main.app, follow_redirects=False, client=(ip, 50000))

    def setUp(self):
        super().setUp(); self.add("alice")

    def test_empty_list_lets_everyone_reach_login(self):
        self.assertEqual(self.client_from("10.9.9.9").get("/login").status_code, 200)

    def test_unlisted_ip_blocked_before_login_everywhere(self):
        db.add_dashboard_ip("192.168.1.0/24")
        stranger = self.client_from("10.9.9.9")
        # Every path answers the same bare 404: nothing reveals that a
        # dashboard, a login, an API or any branding exists here.
        answers = set()
        for path in ("/", "/login", "/settings.html", "/api/health-history", "/api/report",
                     "/app.js", "/assets/enova.png", "/does-not-exist"):
            r = stranger.get(path)
            self.assertEqual(r.status_code, 404, path)
            answers.add(r.text)
            for leak in ("PGuard", "Enova", "login", "Sign in", "password", "Settings", "access"):
                self.assertNotIn(leak.lower(), r.text.lower(), (path, leak))
        self.assertEqual(answers, {"Not Found"})                          # indistinguishable responses
        self.assertEqual(self.login(client=stranger).status_code, 404)    # cannot even try a password
        with self.assertRaises(WebSocketDisconnect):                      # handshake refused, no reason
            with stranger.websocket_connect("/ws/telemetry") as ws:
                ws.receive_json()

    def test_listed_network_and_loopback_allowed(self):
        db.add_dashboard_ip("192.168.1.0/24")
        self.assertEqual(self.client_from("192.168.1.50").get("/login").status_code, 200)
        self.assertEqual(self.client_from("127.0.0.1").get("/login").status_code, 200)  # recovery path

    def test_cannot_lock_yourself_out(self):
        me = self.client_from("10.0.0.7")
        self.login(client=me)
        r = me.post("/api/settings/dashboard-ips", json={"entry": "192.168.5.0/24"})
        self.assertFalse(r.json()["ok"])
        self.assertIn("lock out your own computer", r.json()["error"])
        self.assertTrue(me.post("/api/settings/dashboard-ips", json={"entry": "10.0.0.7", "label": "me"}).json()["ok"])
        self.assertTrue(me.post("/api/settings/dashboard-ips", json={"entry": "192.168.5.0/24"}).json()["ok"])
        self.assertFalse(me.delete("/api/settings/dashboard-ips/10.0.0.7/32").json()["ok"])     # would exclude me
        self.assertTrue(me.delete("/api/settings/dashboard-ips/192.168.5.0/24").json()["ok"])   # CIDR path works
        self.assertEqual(db.dashboard_ip_entries(), ["10.0.0.7/32"])

    def test_invalid_entry_rejected(self):
        self.login()
        self.assertFalse(self.c.post("/api/settings/dashboard-ips", json={"entry": "999.1.1.1"}).json()["ok"])


class TestAuditLog(Base):
    """Hash-chained, append-only audit trail (Settings -> Audit log)."""

    def events(self):
        return [a["event"] for a in reversed(db.audit_list(1000))]

    def test_security_actions_are_recorded_with_actor_and_ip(self):
        self.add("alice")
        self.login(pw="wrong-password")
        self.login()
        self.c.post("/api/settings/trusted-ips", json={"ip": "172.28.0.1"})
        ev = self.events()
        for e in ("login_failed", "login_ok", "trusted_ip_added"):
            self.assertIn(e, ev)
        row = next(a for a in db.audit_list(1000) if a["event"] == "trusted_ip_added")
        self.assertEqual((row["actor"], row["ip"]), ("alice", "testclient"))
        failed = next(a for a in db.audit_list(1000) if a["event"] == "login_failed")
        self.assertEqual(failed["level"], "warning")
        self.assertNotIn("wrong-password", failed["detail"])     # passwords never logged

    def test_lockout_recorded(self):
        self.add()
        for _ in range(policy.get("login_max_fails")):
            self.login(pw="nope-nope")
        self.assertIn("lockout", self.events())

    def test_blocked_computer_recorded_but_throttled(self):
        self.add()
        db.add_dashboard_ip("192.168.1.0/24")
        stranger = TestClient(main.app, follow_redirects=False, client=("10.9.9.9", 1))
        for _ in range(20):
            stranger.get("/")
        self.assertEqual(self.events().count("access_blocked"), 1)  # a scanner cannot flood the table

    def test_chain_verifies_and_detects_edit_and_delete(self):
        for i in range(5):
            db.audit_append("test", detail=str(i))
        self.assertTrue(db.audit_verify()["ok"])
        ids = [a["id"] for a in reversed(db.audit_list(1000))]
        db._conn.execute("UPDATE audit_log SET actor = 'mallory' WHERE id = ?", (ids[2],)); db._conn.commit()
        v = db.audit_verify()
        self.assertFalse(v["ok"]); self.assertEqual(v["broken_at"], ids[2]); self.assertIn("modified", v["reason"])
        db._conn.execute("UPDATE audit_log SET actor = NULL WHERE id = ?", (ids[2],)); db._conn.commit()
        self.assertTrue(db.audit_verify()["ok"])                       # restored -> intact again
        db._conn.execute("DELETE FROM audit_log WHERE id = ?", (ids[1],)); db._conn.commit()
        v = db.audit_verify()
        self.assertFalse(v["ok"]); self.assertEqual(v["broken_at"], ids[2]); self.assertIn("deleted", v["reason"])

    def test_api_read_only_and_behind_login(self):
        self.add()
        self.assertEqual(self.c.get("/api/audit").status_code, 401)
        self.login()
        r = self.c.get("/api/audit").json()
        self.assertTrue(r["integrity"]["ok"])
        self.assertEqual(self.c.delete("/api/audit").status_code, 405)  # no way to erase it from the app


class TestAcknowledgements(Base):
    ALERT = {"key": "hardware_fault:/left_roboteq_driver/status:overheat", "kind": "hardware_fault",
             "severity": "critical", "signal": "left driver overheat", "message": "fault bit set"}

    def setUp(self):
        super().setUp(); self.add("alice"); self.add("bob"); self.login("alice")
        self.bob = TestClient(main.app, follow_redirects=False)
        self.login("bob", client=self.bob)

    def open_keys(self, client=None):
        return {a["alert_key"]: a["acked_by"] for a in (client or self.c).get("/api/acks").json()["open"]}

    def test_ack_is_shared_and_attributed(self):
        self.assertTrue(self.c.post("/api/acks", json=self.ALERT).json()["ok"])
        self.assertEqual(self.open_keys(self.bob), {self.ALERT["key"]: "alice"})   # bob sees alice's ack
        self.assertEqual(self.bob.post("/api/acks", json=self.ALERT).status_code, 409)
        self.assertIn("alert_ack", [a["event"] for a in db.audit_list(100)])

    def test_undo_by_key_with_slashes(self):
        self.c.post("/api/acks", json=self.ALERT)
        r = self.bob.delete("/api/acks/" + self.ALERT["key"])
        self.assertTrue(r.json()["ok"])
        self.assertEqual(self.open_keys(), {})
        row = db.acks_recent(1)[0]
        self.assertEqual((row["resolved_how"], row["resolved_by"]), ("undone", "bob"))

    def tearDown(self):
        auth._active_alerts.clear(); super().tearDown()

    def fire(self, session, state):
        main._log_event(session, "/x", {"key": self.ALERT["key"], "state": state})

    def test_alert_clearing_closes_ack_so_refire_needs_new_ack(self):
        self.fire("s1", "new")
        self.c.post("/api/acks", json=self.ALERT)
        self.fire("s1", "cleared")
        self.assertEqual(self.open_keys(), {})
        self.assertEqual(db.acks_recent(1)[0]["resolved_how"], "cleared")
        self.assertTrue(self.c.post("/api/acks", json=self.ALERT).json()["ok"])  # can ack the refire

    def test_ack_kept_while_any_dashboard_still_shows_the_alert(self):
        self.fire("s1", "new"); self.fire("s2", "new")
        self.c.post("/api/acks", json=self.ALERT)
        self.fire("s1", "cleared")                       # one dashboard cleared it...
        self.assertIn(self.ALERT["key"], self.open_keys())  # ...the other still shows it: ack stays
        auth.end_alert_session("s2", grace_s=0)          # last one disconnects (grace period elapsed)
        self.assertEqual(self.open_keys(), {})
        self.assertEqual(db.acks_recent(1)[0]["resolved_how"], "alert_gone")

    def test_reload_within_grace_keeps_ack(self):
        self.fire("s1", "new")
        self.c.post("/api/acks", json=self.ALERT)
        keys = auth._active_alerts.pop("s1")             # page reloaded: old connection gone...
        self.fire("s2", "new")                           # ...new one re-detects the fault
        auth._close_acks_if_gone(keys, "alert_gone")     # grace timer fires
        self.assertIn(self.ALERT["key"], self.open_keys())

    def test_restart_closes_stale_acks(self):
        self.c.post("/api/acks", json=self.ALERT)
        self.assertEqual(db.acks_resolve_all("restart"), 1)
        self.assertEqual(self.open_keys(), {})

    def test_requires_login_and_json(self):
        anon = TestClient(main.app, follow_redirects=False)
        self.assertEqual(anon.post("/api/acks", json=self.ALERT).status_code, 401)
        r = self.c.post("/api/acks", content="key=x", headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertFalse(r.json()["ok"])


class TestPolicy(Base):
    def setUp(self):
        super().setUp(); self.add("alice"); self.login("alice")

    def post(self, body):
        return self.c.post("/api/settings/policy", json=body)

    def test_defaults_are_the_validated_values(self):
        vals = {p["key"]: p["value"] for p in self.c.get("/api/settings/policy").json()["policy"]}
        self.assertEqual((vals["login_max_fails"], vals["login_lockout_s"], vals["password_min_length"],
                          vals["security_max_cmd_rad_s"]), (5, 60, 8, 240.0))

    def test_cannot_weaken_below_floor_and_all_or_nothing(self):
        for bad in ({"password_min_length": 4}, {"login_lockout_s": 0}, {"login_max_fails": 1000},
                    {"login_max_fails": 4.5}, {"login_max_fails": "x"}, {"login_max_fails": True},
                    {"security_max_cmd_rad_s": float("nan")}, {"nope": 1}):
            r = self.post(bad) if "security_max_cmd_rad_s" not in bad else \
                self.c.post("/api/settings/policy", content='{"security_max_cmd_rad_s": NaN}', headers=JSON)
            self.assertFalse(r.json()["ok"], bad)
        r = self.post({"login_max_fails": 3, "password_min_length": 2})   # one bad value...
        self.assertFalse(r.json()["ok"])
        self.assertEqual(policy.get("login_max_fails"), 5)               # ...nothing applied

    def test_lockout_follows_policy_and_change_is_audited(self):
        self.assertTrue(self.post({"login_max_fails": 3, "login_lockout_s": 120}).json()["ok"])
        other = TestClient(main.app, follow_redirects=False, client=("10.1.1.1", 1))
        for _ in range(3):
            last = self.login("alice", "wrong-pass", client=other)
        self.assertEqual(last.status_code, 429)
        self.assertIn("Locked for 120 s", last.text)
        row = next(a for a in db.audit_list(100) if a["event"] == "policy_changed")
        self.assertIn("login_max_fails 5 -> 3", row["detail"]); self.assertEqual(row["actor"], "alice")

    def test_password_length_policy(self):
        self.post({"password_min_length": 12})
        r = self.c.post("/api/settings/users", json={"username": "bob", "password": "eleven-char"})
        self.assertIn("at least 12", r.json()["error"])

    def test_reset_to_default(self):
        self.post({"login_lockout_s": 300})
        self.assertTrue(self.c.delete("/api/settings/policy/login_lockout_s").json()["ok"])
        self.assertEqual(policy.get("login_lockout_s"), 60)

    def test_security_watch_reads_policy(self):
        from health_monitor import HealthMonitor
        hm = HealthMonitor(security_limits_provider=policy.security_limits)
        self.assertEqual(hm.ingest("/left/front/cmd", {"commanded_velocity": 300.0}, 1.0)[0]["kind"], "security")
        self.post({"security_max_cmd_rad_s": 400})
        hm2 = HealthMonitor(security_limits_provider=policy.security_limits)
        self.assertEqual([e for e in hm2.ingest("/left/front/cmd", {"commanded_velocity": 300.0}, 1.0)
                          if e["kind"] == "security"], [])

    def test_behind_login(self):
        anon = TestClient(main.app, follow_redirects=False)
        self.assertEqual(anon.post("/api/settings/policy", json={"login_max_fails": 20}).status_code, 401)


class TestRobotAddress(Base):
    def setUp(self):
        super().setUp(); self.add("alice"); self.login("alice")
        self._env = {k: os.environ.pop(k, None) for k in ("ROSBRIDGE_HOST", "ROSBRIDGE_PORT", "SPEEDTEST_HOST", "SPEEDTEST_PORT")}

    def tearDown(self):
        for k, v in self._env.items():
            if v is not None:
                os.environ[k] = v
        super().tearDown()

    def test_priority_default_env_settings(self):
        self.assertEqual((policy.robot()["host"], policy.robot()["source"]), ("localhost", "default"))
        os.environ["ROSBRIDGE_HOST"] = "10.0.0.9"
        self.assertEqual((policy.robot()["host"], policy.robot()["source"]), ("10.0.0.9", "env"))
        r = self.c.post("/api/settings/robot", json={"host": "192.168.1.50", "port": 9091, "speedtest_port": 8092}).json()
        self.assertTrue(r["ok"])
        rb = policy.robot()
        self.assertEqual((rb["host"], rb["port"], rb["speedtest_host"], rb["speedtest_port"], rb["source"]),
                         ("192.168.1.50", 9091, "192.168.1.50", 8092, "settings"))
        del os.environ["ROSBRIDGE_HOST"]
        self.assertTrue(self.c.delete("/api/settings/robot").json()["ok"])
        self.assertEqual(policy.robot()["source"], "default")

    def test_validation(self):
        for body in ({"host": "", "port": 9090, "speedtest_port": 8091},
                     {"host": "http://evil/x", "port": 9090, "speedtest_port": 8091},
                     {"host": "a b", "port": 9090, "speedtest_port": 8091},
                     {"host": "robot.local", "port": 70000, "speedtest_port": 8091},
                     {"host": "robot.local", "port": "9090; rm", "speedtest_port": 8091}):
            self.assertFalse(self.c.post("/api/settings/robot", json=body).json()["ok"], body)
        self.assertTrue(self.c.post("/api/settings/robot",
                                    json={"host": "PGuard-01.local", "port": 9090, "speedtest_port": 8091}).json()["ok"])
        self.assertEqual(policy.robot()["host"], "pguard-01.local")

    def test_change_audited_and_open_sessions_told_to_reconnect(self):
        import asyncio
        q = asyncio.Queue()
        main._live_queues["s1"] = q
        try:
            r = self.c.post("/api/settings/robot", json={"host": "10.0.0.5", "port": 9090, "speedtest_port": 8091}).json()
            self.assertEqual(r["reconnected"], 1)
            kind, msg, *_ = q.get_nowait()
            self.assertEqual(kind, "reconnect"); self.assertIn("10.0.0.5:9090", msg)
        finally:
            main._live_queues.pop("s1", None)
        row = next(a for a in db.audit_list(100) if a["event"] == "robot_address_changed")
        self.assertIn("-> 10.0.0.5:9090", row["detail"])

    def test_connection_test_endpoint(self):
        import socket
        srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
        port = srv.getsockname()[1]
        try:
            ok = self.c.post("/api/settings/robot/test", json={"host": "127.0.0.1", "port": port, "speedtest_port": 8091}).json()
            self.assertTrue(ok["reachable"])
        finally:
            srv.close()
        bad = self.c.post("/api/settings/robot/test", json={"host": "127.0.0.1", "port": port, "speedtest_port": 8091}).json()
        self.assertFalse(bad["reachable"])
        self.assertEqual(policy.robot()["host"], "localhost")   # testing never saves


class TestRoles(Base):
    """admin vs operator - enforced in the middleware for every request."""
    ADMIN_ROUTES = [
        ("GET", "/api/settings/policy", None), ("POST", "/api/settings/policy", {"login_max_fails": 20}),
        ("DELETE", "/api/settings/policy/login_max_fails", None),
        ("POST", "/api/settings/users", {"username": "mallory", "password": "long enough pw", "role": "admin"}),
        ("DELETE", "/api/settings/users/alice", None),
        ("POST", "/api/settings/users/bob/role", {"role": "admin"}),
        ("POST", "/api/settings/trusted-ips", {"ip": "6.6.6.6"}), ("DELETE", "/api/settings/trusted-ips/1.1.1.1", None),
        ("POST", "/api/settings/dashboard-ips", {"entry": "6.6.6.0/24"}),
        ("DELETE", "/api/settings/dashboard-ips/1.1.1.0/24", None),
        ("POST", "/api/settings/robot", {"host": "6.6.6.6", "port": 9090, "speedtest_port": 8091}),
        ("DELETE", "/api/settings/robot", None),
        ("POST", "/api/settings/robot/test", {"host": "127.0.0.1", "port": 1, "speedtest_port": 8091}),
        ("GET", "/api/audit", None), ("GET", "/api/audit/", None), ("GET", "/api/settings/", None),
    ]

    def setUp(self):
        super().setUp(); self.add("alice"); self.add("bob")   # first account = admin, then operator
        self.login("alice")
        self.op = TestClient(main.app, follow_redirects=False)
        self.login("bob", client=self.op)

    def test_first_account_is_admin_next_default_operator(self):
        self.assertEqual({u["username"]: u["role"] for u in db.list_users()}, {"alice": "admin", "bob": "operator"})

    def test_operator_blocked_from_every_admin_route(self):
        for method, path, body in self.ADMIN_ROUTES:
            r = self.op.request(method, path, json=body) if body else self.op.request(method, path)
            self.assertEqual(r.status_code, 403, (method, path))
        self.assertEqual(policy.get("login_max_fails"), 5)
        self.assertEqual([u["username"] for u in db.list_users()], ["alice", "bob"])
        self.assertEqual(db.trusted_ip_set(), set())
        self.assertEqual(policy.robot()["host"] != "6.6.6.6", True)
        denied = [a for a in db.audit_list(500) if a["event"] == "admin_denied"]
        self.assertEqual(len(denied), len(self.ADMIN_ROUTES)); self.assertEqual(denied[0]["actor"], "bob")

    def test_operator_sees_only_own_account_and_can_do_their_job(self):
        st = self.op.get("/api/settings").json()
        self.assertEqual((st["me"], st["role"]), ("bob", "operator"))
        for k in ("users", "trusted_ips", "dashboard_ips", "your_ip"):
            self.assertNotIn(k, st)
        self.assertTrue(self.op.post("/api/settings/password",
                                     json={"current": self.PASSWORD, "new": "bob new password"}).json()["ok"])
        for path in ("/", "/settings.html", "/api/health-history", "/api/acks", "/api/report?format=html"):
            self.assertEqual(self.op.get(path).status_code, 200, path)
        ack = {"key": "k1", "kind": "comms", "severity": "warning", "signal": "s", "message": "m"}
        self.assertTrue(self.op.post("/api/acks", json=ack).json()["ok"])
        self.assertEqual(self.op.get("/api/auth-status").json()["role"], "operator")

    def test_admin_promotes_and_demotion_applies_immediately(self):
        self.assertTrue(self.c.post("/api/settings/users/bob/role", json={"role": "admin"}).json()["ok"])
        self.assertEqual(self.op.get("/api/audit").status_code, 200)            # same session, new rights
        self.assertTrue(self.c.post("/api/settings/users/bob/role", json={"role": "operator"}).json()["ok"])
        self.assertEqual(self.op.get("/api/audit").status_code, 403)            # revoked at once
        row = next(a for a in db.audit_list(100) if a["event"] == "role_changed")
        self.assertIn("admin -> operator", row["detail"])

    def test_last_admin_protected(self):
        # The sole admin cannot demote or delete themself - so the dashboard
        # can never end up with nobody able to manage it.
        self.assertFalse(self.c.post("/api/settings/users/alice/role", json={"role": "operator"}).json()["ok"])
        self.assertFalse(self.c.delete("/api/settings/users/alice").json()["ok"])
        # Hand-over works: promote bob, bob demotes alice; now bob is alone
        # and the same rules protect him.
        self.c.post("/api/settings/users/bob/role", json={"role": "admin"})
        self.assertTrue(self.op.post("/api/settings/users/alice/role", json={"role": "operator"}).json()["ok"])
        self.assertEqual(self.c.post("/api/settings/users/bob/role", json={"role": "operator"}).status_code, 403)
        self.assertFalse(self.op.post("/api/settings/users/bob/role", json={"role": "operator"}).json()["ok"])
        self.assertEqual(db.count_admins(), 1)

    def test_last_admin_rule_backstop(self):
        # Defence in depth: even if the own-account rule were bypassed, the
        # handler itself refuses to remove the last admin.
        class Req:
            cookies = {}
            client = None
        orig = auth.current_user
        auth.current_user = lambda request: "someone-else"
        try:
            self.assertIn("last admin", auth.delete_user(Req(), "alice").body.decode())
        finally:
            auth.current_user = orig

    def test_cannot_delete_or_demote_last_admin_via_other_admin_path(self):
        db.add_user("carol", *auth.hash_password("whatever pw"), role="admin")
        self.c.delete("/api/settings/users/carol")      # fine: alice stays admin
        self.assertEqual(db.count_admins(), 1)
        self.assertFalse(self.c.post("/api/settings/users/nobody/role", json={"role": "admin"}).json()["ok"])
        self.assertFalse(self.c.post("/api/settings/users/bob/role", json={"role": "root"}).json()["ok"])

    def test_admin_creates_admin_and_bad_role_refused(self):
        self.assertFalse(self.c.post("/api/settings/users", json={"username": "dave", "password": "long enough pw",
                                                                  "role": "superuser"}).json()["ok"])
        self.assertTrue(self.c.post("/api/settings/users", json={"username": "dave", "password": "long enough pw",
                                                                 "role": "admin"}).json()["ok"])
        self.assertEqual(db.get_user("dave")["role"], "admin")

    def test_migration_makes_existing_accounts_admins(self):
        conn = sqlite3.connect(os.path.join(self._tmp.name, "old.db"))
        conn.execute("CREATE TABLE users (username TEXT PRIMARY KEY, pw_hash TEXT NOT NULL, salt TEXT NOT NULL, "
                     "iterations INTEGER NOT NULL, created_at REAL NOT NULL, created_by TEXT)")
        conn.execute("INSERT INTO users VALUES ('old', 'h', 's', 1, 0, NULL)"); conn.commit()
        db._init_schema(conn)
        self.assertEqual(conn.execute("SELECT role FROM users").fetchone()[0], "admin")
        conn.close()


if __name__ == "__main__":
    unittest.main()
