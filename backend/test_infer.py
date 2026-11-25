"""Unit tests for ml/infer.py + HealthMonitor category-9 wiring.

Fast only (synthetic ticks, real artifacts - no bag replay). Run from
backend/:  python -m unittest test_infer -v
Skipped entirely if ml/artifacts isn't built.
"""
import unittest
from pathlib import Path

import numpy as np

ARTIFACTS = Path(__file__).parent.parent / "ml" / "artifacts"
META_OK = (ARTIFACTS / "metadata.json").exists()

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from ml.infer import (  # noqa: E402
    MLScorer, ML_TOPICS, RAW_FIELDS, _MOTORS,
)


def _fb(motor_current=1.0, supply=52.5, sup_current=0.0, temp=45.0):
    return {"motor_current": motor_current, "supply_voltage": supply,
            "supply_current": sup_current, "channel_temperature": temp,
            "motor_temperature": -4.1754, "measured_velocity": 0.0}


_FB_TOPICS = ["/left/front/feedback", "/left/rear/feedback",
              "/right/front/feedback", "/right/rear/feedback"]


def _feed_full_round(scorer, t, bat_v=53.0, bat_i=-4.0, sup=52.5):
    """One message per ML data topic at time t; returns tick verdicts."""
    out = []
    r = scorer.update("/battery/state",
                      {"voltage": bat_v, "current": bat_i,
                       "percentage": 70.0}, t)
    if r is not None:
        out.append(r)
    for topic in _FB_TOPICS:
        r = scorer.update(topic, _fb(supply=sup), t)
        if r is not None:
            out.append(r)
    return out


@unittest.skipUnless(META_OK, "ml/artifacts not built - run ml/train.py first")
class TestScorerBasics(unittest.TestCase):
    def test_loads_and_gates_schema(self):
        s = MLScorer()
        self.assertEqual(s.tick_dt, 0.2)
        self.assertEqual(s.ae_window, 20)
        self.assertIn("idle", s.thr_if)
        self.assertIn("drive", s.thr_if)

    def test_tick_gating_and_staleness(self):
        s = MLScorer()
        # first round: cache fills, tick fires once complete
        v = _feed_full_round(s, 1000.0)
        self.assertTrue(v)
        # immediate re-feed: no tick (0.2s gate)
        v2 = _feed_full_round(s, 1000.05)
        self.assertFalse(v2)
        # next gate slot ticks
        v3 = _feed_full_round(s, 1000.25)
        self.assertTrue(v3)
        # battery last seen at 1000.25; feedback-only at 1010.0 means a
        # 9.75s-old battery (>2s max_age): ticks stop, no junk scored
        out = [s.update(topic, _fb(), 1010.0) for topic in _FB_TOPICS]
        self.assertEqual(out, [None] * 4)

    def test_nonfinite_holds_last_good(self):
        # NaN/missing readings must not poison the cache: the tick holds
        # the last good value (within max_age) with a finite vector,
        # instead of scoring NaN or crashing. Past max_age, the staleness
        # gate above takes over and stops ticks.
        s = MLScorer()
        _feed_full_round(s, 1000.0)
        r = s.update("/battery/state",
                     {"voltage": 53.0, "current": float("nan"),
                      "percentage": 70.0}, 1000.25)
        rs = [s.update(topic, _fb(), 1000.25) for topic in _FB_TOPICS]
        # the battery message itself ticked on the held last-good current
        # (0.25s gate had passed); the 4 same-t feedbacks correctly yield
        # nothing further. Vector finite throughout - NaN never poisoned it.
        self.assertIsNotNone(r)
        self.assertEqual(rs, [None] * 4)
        self.assertTrue(np.isfinite(s._assemble()).all())

    def test_vdrop_uses_raw_values(self):
        # Sub-floor supply readings must reach vdrop uncleaned: a 2V
        # dropout is vdrop ~= -51V, NOT a carried-forward normal value.
        # (Guards the Phase 2 finding: clean_supply_voltage() must never
        # touch this path.)
        s = MLScorer()
        _feed_full_round(s, 1000.0)
        s.update("/battery/state",
                 {"voltage": 53.0, "current": -4.0, "percentage": 70.0},
                 1000.25)
        for topic in _FB_TOPICS:
            s.update(topic, _fb(supply=2.0), 1000.25)
        vec = s._assemble()
        vdrop = vec[9:13]
        for v in vdrop:
            self.assertAlmostEqual(v, 2.0 - 53.0, places=6)

    def test_ae_cold_start_and_stride(self):
        s = MLScorer()
        eval_ticks = []
        for i in range(40):
            for v in _feed_full_round(s, 1000.0 + i * 0.25):
                if v["ae_score"] is not None:
                    eval_ticks.append(v["t"])
        # AE joins once 20 vectors buffered, then evaluates every 5th tick
        self.assertTrue(eval_ticks)
        self.assertGreaterEqual(eval_ticks[0], 1000.0 + 19 * 0.25 - 0.01)
        gaps = [round(b - a, 2) for a, b in
                zip(eval_ticks, eval_ticks[1:])]
        self.assertTrue(all(abs(g - 1.25) < 0.3 for g in gaps),
                        f"AE eval stride should be ~1.25s, got {gaps[:5]}")

    def test_driving_flag_from_cmd(self):
        s = MLScorer()
        _feed_full_round(s, 1000.0)
        self.assertFalse(s._is_driving())
        s.update("/left/front/cmd", {"commanded_velocity": 50.0}, 1000.1)
        self.assertTrue(s._is_driving())
        s.update("/left/front/cmd", {"commanded_velocity": 0.0}, 1000.2)
        self.assertFalse(s._is_driving())

    def test_verdict_carries_norms(self):
        s = MLScorer()
        seen = None
        for i in range(30):
            for v in _feed_full_round(s, 1000.0 + i * 0.25):
                seen = v
        self.assertIsNotNone(seen)
        self.assertAlmostEqual(seen["if_norm"],
                               seen["if_score"] / s.thr_if["idle"])
        self.assertIn("ae_norm", seen)
        self.assertIn("top_feature", seen)

    def test_top_feature_tracks_spike(self):
        import numpy as np
        s = MLScorer()
        vec = np.zeros(13)
        vec[1] = 50.0  # motor_LF_current far outside any fitted range
        self.assertEqual(s._top_feature(vec), "motor_LF_current")
        vec2 = np.zeros(13)
        vec2[0] = -500.0  # bat_current
        self.assertEqual(s._top_feature(vec2), "bat_current")

    def test_raw_fields_match_train_sources(self):
        # (Imports torch via train.py; installed per requirements.)
        sys.path.insert(0, str(Path(__file__).parent.parent / "ml"))
        import train
        expect = {("bat_voltage", "/battery/state", "voltage"),
                  ("bat_current", "/battery/state", "current")}
        for m in ("LF", "LR", "RF", "RR"):
            t = train.FEATURE_SRC[f"motor_{m}_supply_voltage"][0]
            expect |= {(f"{m}_motor_current", t, "motor_current"),
                       (f"{m}_supply_current", t, "supply_current"),
                       (f"{m}_supply_voltage_raw", t, "supply_voltage")}
        self.assertEqual(set(RAW_FIELDS), expect)


@unittest.skipUnless(META_OK, "ml/artifacts not built - run ml/train.py first")
class TestCategory9Wiring(unittest.TestCase):
    def test_fused_verdict_debounces_and_never_escalates(self):
        from health_monitor import HealthMonitor
        mon = HealthMonitor()
        self.assertIsNotNone(mon._ml)

        class FakeML:
            def __init__(self):
                self.calls = 0

            def update(self, topic, data, t):
                self.calls += 1
                return {"t": t, "driving": False, "if_score": 9.0,
                        "if_flag": True, "if_norm": 9.0,
                        "ae_score": None, "ae_ready": False,
                        "ae_live": False, "ae_flag": False, "ae_norm": None,
                        "top_feature": "motor_LF_current",
                        "fused": True, "contributors": ["IF"]}

        mon._ml = FakeML()
        evs = []
        for i in range(30):
            evs.extend(mon.ingest("/battery/state",
                                  {"voltage": 53.0, "current": -4.0,
                                   "percentage": 70.0}, 1000.0 + i * 0.25))
        new = [e for e in evs if e["key"] == "ml:anomaly"
               and e["state"] == "new"]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["kind"], "ml")
        self.assertEqual(new[0]["severity"], "warning")
        self.assertIsNone(new[0]["capability"])
        self.assertIn("IF", new[0]["message"])
        self.assertIn("largest deviation: motor_LF_current", new[0]["message"])
        self.assertIn("x)", new[0]["message"])  # normalized multiples shown
        # capability=None => failure escalation skips it, even sustained
        evs2 = []
        for i in range(30, 400):
            evs2.extend(mon.ingest("/battery/state",
                                   {"voltage": 53.0, "current": -4.0,
                                    "percentage": 70.0},
                                   1000.0 + i * 0.25))
        kinds = {e["kind"] for e in evs2}
        self.assertNotIn("failure", kinds)

    def test_ae_only_abstains_from_attribution(self):
        # AE window-shape flags have no single culprit row by
        # construction: the message must say so, never invent one.
        from health_monitor import HealthMonitor
        mon = HealthMonitor()

        class FakeAE:
            def update(self, topic, data, t):
                return {"t": t, "driving": True, "if_score": 0.1,
                        "if_flag": False, "if_norm": 0.2,
                        "ae_score": 500.0, "ae_ready": True,
                        "ae_live": True, "ae_flag": True, "ae_norm": 3.1,
                        "top_feature": None,
                        "fused": True, "contributors": ["AE"]}

        mon._ml = FakeAE()
        evs = []
        for i in range(30):
            evs.extend(mon.ingest("/battery/state",
                                  {"voltage": 53.0, "current": -4.0,
                                   "percentage": 70.0}, 1000.0 + i * 0.25))
        new = [e for e in evs if e["key"] == "ml:anomaly"
               and e["state"] == "new"]
        self.assertEqual(len(new), 1)
        self.assertIn("shape anomaly (no single culprit)", new[0]["message"])
        self.assertNotIn("largest deviation", new[0]["message"])

    def test_non_ml_topics_ignored(self):
        from health_monitor import HealthMonitor
        mon = HealthMonitor()
        calls = []
        real_update = mon._ml.update if mon._ml else None
        if real_update is None:
            self.skipTest("no artifacts")
        mon._ml.update = lambda *a: (calls.append(a), None)[1]
        mon.ingest("/ublox/gnss_status", {"fixType": 4}, 1000.0)
        mon.ingest("/nmea_sentence", {"sentence": "x"}, 1000.1)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
