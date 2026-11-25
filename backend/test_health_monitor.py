"""Unit tests for backend/health_monitor.py. Stdlib unittest only.

Covers the pure-logic core: RollingStat math, debounce transitions,
hardware-fault decode, fault-window baseline suppression, GNSS gate,
residual warm-up replay, and the KNOWN_ABSENT coverage rule. Run from
backend/:  python -m unittest test_health_monitor -v

The full-bag golden replay (tests against backend/data/*.json, ~2 min)
is in TestGoldenReplay, skipped unless PGUARD_GOLDEN=1 is set - it pins
the exact event counts so a future change that shifts detection behavior
fails loudly instead of silently.
"""
import os
import unittest

from health_monitor import (
    HealthMonitor, RollingStat, MIN_BASELINE_SAMPLES, DEBOUNCE_SECONDS,
    GNSS_DEGRADED_FIX,
)


def feed(mon, topic, data, t0=1000.0, n=30, dt=0.1):
    """Ingest n messages; return all emitted events. `data` is a dict or a
    function(i) -> dict. NOTE: baselines need slight variation - a
    perfectly flat baseline has std ~= 0, and zscore() returns 0.0 by
    design (quantization floor), so a later jump would never fire."""
    out = []
    for i in range(n):
        d = data(i) if callable(data) else data
        out.extend(mon.ingest(topic, dict(d), t0 + i * dt))
    return out


def battery(v):
    return {"voltage": v, "current": -4.0, "percentage": 70.0}


class TestRollingStat(unittest.TestCase):
    def test_zscore_needs_baseline(self):
        s = RollingStat()
        s.push(0.0, 1.0)
        self.assertIsNone(s.zscore(999.0))

    def test_zscore_flags_outlier(self):
        s = RollingStat()
        # varying baseline (identical values would give std ~= 0 and z = 0
        # by design - see the quantization floor in RollingStat)
        for i in range(MIN_BASELINE_SAMPLES + 5):
            s.push(float(i) * 0.1, 10.0 + (i % 5) * 0.1)
        self.assertAlmostEqual(s.zscore(10.2), 0.0, delta=1.0)
        self.assertGreater(abs(s.zscore(20.0)), 3.5)

    def test_is_flat(self):
        s = RollingStat()
        for i in range(MIN_BASELINE_SAMPLES + 5):
            s.push(float(i), 5.0)
        self.assertTrue(s.is_flat(MIN_BASELINE_SAMPLES, 1.0))
        s.push(999.0, 6.0)
        # window still mostly flat but not bit-identical anymore
        self.assertFalse(s.is_flat(MIN_BASELINE_SAMPLES, 1.0))


class TestDebounce(unittest.TestCase):
    def test_single_blip_does_not_fire(self):
        mon = HealthMonitor()
        feed(mon, "/battery/state",
             lambda i: battery(53.0 + (i % 2) * 0.1), n=30)
        evs = mon.ingest("/battery/state",
                         {"voltage": 80.0, "current": -4.0, "percentage": 70.0},
                         1003.1)
        self.assertFalse([e for e in evs if e["kind"] == "statistical"
                          and e["state"] == "new"])

    def test_sustained_anomaly_fires(self):
        mon = HealthMonitor()
        # NOTE: dt=1.0s on purpose. With a tiny 30-sample baseline, a huge
        # step at 10Hz self-normalizes (z decays below threshold after a
        # few samples) before the 2s wall-clock debounce completes.
        # Production baselines hold hundreds of samples so adaptation is
        # slow; 1s spacing reproduces that ratio honestly in miniature.
        feed(mon, "/battery/state",
             lambda i: battery(53.0 + (i % 2) * 0.1), n=30, dt=1.0)
        # Anomaly run must also start within WINDOW_SECONDS of the
        # baseline - a wider gap would age it out of the window (correct
        # production behavior, bad test design).
        evs = feed(mon, "/battery/state",
                   {"voltage": 80.0, "current": -4.0, "percentage": 70.0},
                   t0=1035.0, n=30, dt=1.0)
        new = [e for e in evs if e["kind"] == "statistical"
               and e["state"] == "new"]
        self.assertTrue(new)
        # debounce cost is bounded: fired within ~DEBOUNCE of the run start
        self.assertLessEqual(new[0]["started_at"] - 1035.0,
                             DEBOUNCE_SECONDS + 1.5)


class TestHardwareFault(unittest.TestCase):
    def test_decode_and_clear(self):
        mon = HealthMonitor()
        evs = mon.ingest("/left_roboteq_driver/status",
                         {"fault": 4, "status": 129}, 1000.0)
        new = [e for e in evs if e["kind"] == "hardware_fault"
               and e["state"] == "new"]
        self.assertEqual(len(new), 1)
        self.assertIn("UNDERVOLTAGE", new[0]["message"])
        self.assertEqual(new[0]["severity"], "critical")
        # repeat: no duplicate
        self.assertEqual(mon.ingest("/left_roboteq_driver/status",
                                    {"fault": 4, "status": 129}, 1001.0), [])
        # clear
        evs = mon.ingest("/left_roboteq_driver/status",
                         {"fault": 0, "status": 129}, 1002.0)
        self.assertTrue([e for e in evs if e["state"] == "cleared"])


class TestFaultSuppression(unittest.TestCase):
    FB_L = "/left/front/feedback"
    FB_R = "/right/front/feedback"

    def _normal_fb(self, v=1.0):
        return {"motor_current": v, "channel_temperature": 45.0,
                "supply_voltage": 52.5, "motor_temperature": -4.1754,
                "measured_velocity": 0.0}

    def _normal_fb_varying(self, i, v=1.0):
        d = self._normal_fb(v + (i % 2) * 0.1)
        return d

    def test_faulted_side_not_learned_or_judged(self):
        mon = HealthMonitor()
        feed(mon, self.FB_L, self._normal_fb_varying, n=30)
        uni_key = f"stat:{self.FB_L}:motor_current"
        split_key = f"{uni_key}:idle"  # no driving in this test
        n_uni_before = len(mon._stats[uni_key].samples)
        n_split_before = len(mon._stats[split_key].samples)
        self.assertGreaterEqual(n_split_before, MIN_BASELINE_SAMPLES)
        mon.ingest("/left_roboteq_driver/status", {"fault": 4}, 1010.0)
        evs = feed(mon, self.FB_L,
                   self._normal_fb(v=500.0), t0=1011.0, n=30)
        self.assertFalse([e for e in evs if e["kind"] == "statistical"])
        # split (judgement) pool untouched by the fault...
        self.assertEqual(len(mon._stats[split_key].samples), n_split_before)
        # ...while the unified (liveness-only, never z-scored) pool keeps
        # feeding so dead-sensor detection stays live through the fault
        self.assertEqual(len(mon._stats[uni_key].samples), n_uni_before + 30)
        # clear the fault: normal data is judged against the intact
        # pre-fault baseline - no events, no contamination hangover
        mon.ingest("/left_roboteq_driver/status", {"fault": 0}, 1020.0)
        evs = feed(mon, self.FB_L, self._normal_fb_varying, t0=1021.0, n=30)
        self.assertFalse([e for e in evs if e["kind"] == "statistical"])

    def test_other_side_unaffected(self):
        mon = HealthMonitor()
        mon.ingest("/left_roboteq_driver/status", {"fault": 4}, 1000.0)
        feed(mon, self.FB_R, self._normal_fb_varying, t0=1001.0, n=30,
             dt=1.0)
        evs = feed(mon, self.FB_R,
                   lambda i: self._normal_fb_varying(i, v=500.0),
                   t0=1040.0, n=30, dt=1.0)
        self.assertTrue([e for e in evs if e["kind"] == "statistical"
                         and e["state"] == "new"])


class TestGnss(unittest.TestCase):
    def test_sustained_loss_fires_and_recovers(self):
        mon = HealthMonitor()
        evs = feed(mon, "/ublox/gnss_status", {"fixType": 1},
                   t0=1000.0, n=30, dt=0.1)
        new = [e for e in evs if e["kind"] == "gnss" and e["state"] == "new"]
        self.assertTrue(new)
        self.assertIn("DEAD_RECKONING", new[0]["message"])
        evs = feed(mon, "/ublox/gnss_status", {"fixType": 4},
                   t0=1010.0, n=30, dt=0.1)
        self.assertTrue([e for e in evs if e["kind"] == "gnss"
                         and e["state"] == "cleared"])

    def test_single_blip_does_not_fire(self):
        # receiver flicker (29/44 degraded episodes under 2s in the
        # reference bag) must not flap the feed
        mon = HealthMonitor()
        evs = feed(mon, "/ublox/gnss_status", {"fixType": 0},
                   t0=1000.0, n=5, dt=0.1)
        self.assertFalse([e for e in evs if e["kind"] == "gnss"])

    def test_usable_position_is_healthy(self):
        # 2D and GNSS+DR are 95.6% of the reference bag: normal operation,
        # not degradation. TIME_ONLY (5) is the ordinal-threshold bug
        # regression case: 5 > 3 but worse than 2D.
        for fix in (2, 3, 4):
            mon = HealthMonitor()
            evs = feed(mon, "/ublox/gnss_status", {"fixType": fix},
                       t0=1000.0, n=40, dt=0.1)
            self.assertFalse([e for e in evs if e["kind"] == "gnss"],
                             f"fixType {fix} should be healthy")
        mon = HealthMonitor()
        evs = feed(mon, "/ublox/gnss_status", {"fixType": 5},
                   t0=1000.0, n=40, dt=0.1)
        new = [e for e in evs if e["kind"] == "gnss" and e["state"] == "new"]
        self.assertTrue(new)
        self.assertIn("TIME_ONLY", new[0]["message"])


class TestResidualWarmup(unittest.TestCase):
    def test_merge_walk_seeds_baseline_and_last_cmd(self):
        mon = HealthMonitor()
        cmd = [(1000.0 + i, {"commanded_velocity": 10.0}) for i in range(40)]
        fb = [(1000.0 + i + 0.05, {"measured_velocity": 10.0 + (i % 3) * 0.1})
              for i in range(40)]
        mon.warm_up({"/left/front/cmd": cmd,
                     "/left/front/feedback": fb})
        self.assertEqual(mon._last_cmd.get("left_front"), 10.0)
        self.assertGreaterEqual(
            len(mon._stats["residual:left_front"].samples), 30)
        # no events ever emitted by warm-up
        self.assertEqual(mon._active, {})


class TestKnownAbsent(unittest.TestCase):
    def test_battery_temperature_not_warned(self):
        mon = HealthMonitor()
        feed(mon, "/battery/state",
             {"voltage": 53.0, "current": -4.0, "percentage": 70.0}, n=110)
        warned = {(w["topic"], w["field"])
                  for w in mon.field_coverage_warnings()}
        self.assertNotIn(("/battery/state", "temperature"), warned)


class TestProvenDead(unittest.TestCase):
    FB = "/left/front/feedback"

    def _const_fb(self, t0=1000.0, n=100, dt=1.0, motor_temp=-4.1754):
        mon = HealthMonitor()
        evs = []
        for i in range(n):
            evs.extend(mon.ingest(
                self.FB,
                {"motor_current": 1.0 + (i % 2) * 0.1,
                 "channel_temperature": 45.0,
                 "supply_voltage": 52.5, "motor_temperature": motor_temp,
                 "measured_velocity": 0.0},
                t0 + i * dt))
        return mon, evs

    def test_sentinel_fires_sensor_failure_and_escalates(self):
        # motor_temperature == registered sentinel, flat 100s:
        # kind sensor_failure, critical, supervision capability, and an
        # immediate failure event (90s span IS the persistence proof).
        mon, evs = self._const_fb()
        new = [e for e in evs if e["key"] == f"sensor:{self.FB}:motor_temperature"
               and e["state"] == "new"]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["kind"], "sensor_failure")
        self.assertEqual(new[0]["severity"], "critical")
        self.assertEqual(new[0]["capability"], "supervision")
        self.assertIn("sentinel", new[0]["message"])
        fail = [e for e in evs if e["kind"] == "failure"
                and "supervision" in e.get("capability", "")]
        self.assertTrue(fail)

    def test_non_sentinel_flat_stays_suspect(self):
        # Same flatness, ordinary value: sensor_health warning, no
        # capability, and still no failure after long sustain.
        mon, evs = self._const_fb(motor_temp=45.0)
        new = [e for e in evs
               if e["key"] == f"sensor:{self.FB}:motor_temperature"
               and e["state"] == "new"]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["kind"], "sensor_health")
        self.assertIsNone(new[0]["capability"])
        kinds = {e["kind"] for e in evs}
        self.assertNotIn("failure", kinds)

    def test_suspect_clears_on_movement(self):
        mon, evs = self._const_fb(motor_temp=45.0)
        self.assertTrue([e for e in evs if e["state"] == "new"])
        evs2 = []
        for i in range(30):
            evs2.extend(mon.ingest(
                self.FB,
                {"motor_current": 1.0, "channel_temperature": 45.0,
                 "supply_voltage": 52.5,
                 "motor_temperature": 45.0 + i * 0.5,
                 "measured_velocity": 0.0},
                1200.0 + i * 0.5))
        self.assertTrue([e for e in evs2 if e["state"] == "cleared"])


class TestLivenessRate(unittest.TestCase):
    def test_high_rate_flat_still_detected(self):
        # Programmer-regression test for the live-session bug: at 50Hz
        # native publish rate the old 900-capped pools covered ~18s, so the
        # 90s span could never fill and dead sensors were undetectable
        # live (warm-up seeds fired once, then eviction cleared them ~17s
        # later). Stride pools span by wall-clock at any rate.
        mon = HealthMonitor()
        evs = []
        for i in range(5000):  # 100s at 50Hz
            evs.extend(mon.ingest(
                "/left/front/feedback",
                {"motor_current": 1.0, "channel_temperature": 45.0,
                 "supply_voltage": 52.5, "motor_temperature": -4.1754,
                 "measured_velocity": 0.0},
                1000.0 + i * 0.02))
        new = [e for e in evs
               if e["key"] == "sensor:/left/front/feedback:motor_temperature"
               and e["state"] == "new"]
        self.assertTrue(new)
        # and it stays dead (no spurious clear while still flat)
        self.assertFalse([e for e in evs
                          if e["key"] == "sensor:/left/front/feedback:motor_temperature"
                          and e["state"] == "cleared"])


class TestLivenessSeesBriefChanges(unittest.TestCase):
    # channel_temperature is driving-gated: only samples taken while some
    # motor has a nonzero command are judged, so each run starts driving.
    KEY = "sensor:/left/front/feedback:channel_temperature"

    def _drive(self, temp_at):
        mon = HealthMonitor()
        evs = mon.ingest("/left/front/cmd", {"commanded_velocity": 50.0}, 999.0)
        for i in range(6000):  # 120s at 50Hz, robot driving throughout
            t = 1000.0 + i * 0.02
            evs += mon.ingest("/left/front/feedback",
                              {"motor_current": 1.0 + 0.01 * (i % 5), "channel_temperature": temp_at(t),
                               "supply_voltage": 52.5, "measured_velocity": 50.0,
                               "commanded_velocity": 50.0}, t)
        return [e for e in evs if e["key"] == self.KEY and e["state"] == "new"]

    def test_change_between_strides_is_not_flat(self):
        # Golden-run regression: a reading that sits at 57 but dips to 56
        # for a few samples BETWEEN two 1s stride instants was invisible to
        # the stride pool, so a working sensor read as flat for 90s+ and
        # fired sensor_health. Every dip must reach the pool.
        self.assertEqual(self._drive(lambda t: 56.0 if 0.40 <= (t - 1000.0) % 1.0 < 0.50 else 57.0), [])

    def test_truly_flat_while_driving_still_detected(self):
        # Control: a channel temperature that never moves under load still fires.
        self.assertTrue(self._drive(lambda t: 57.0))


class TestCommsExemptTopics(unittest.TestCase):
    def _run(self, quiet_topic):
        # quiet_topic publishes at 10Hz for 3s, then goes silent while a
        # battery stream keeps arriving for another 20s (well past the 5s
        # floor) - the comms check runs on every battery arrival.
        mon = HealthMonitor()
        evs = []
        for i in range(30):
            evs += mon.ingest(quiet_topic, {"data": "x"}, 1000.0 + i * 0.1)
        for i in range(200):
            evs += mon.ingest("/battery/state", battery(53.0 + 0.01 * (i % 3)), 1003.0 + i * 0.1)
        return [e for e in evs if e["key"] == f"comms:{quiet_topic}" and e["state"] == "new"]

    def test_rosout_silence_is_not_a_comms_failure(self):
        # Live-run regression: /rosout and /rosout_agg only publish when a
        # node logs, so they went "stale" seconds after every connect.
        for topic in ("/rosout", "/rosout_agg", "/client_count", "/connected_clients"):
            self.assertEqual(self._run(topic), [], topic)

    def test_robot_topic_silence_still_fires(self):
        # Control: the exemption must not blind the check for robot topics.
        self.assertTrue(self._run("/ublox/gnss_status"))


class TestGoldenReplay(unittest.TestCase):
    """Full reference-bag replay pins exact event counts. Slow (~15 min
    with category-9 ML wired: ~63k ticks x ~10ms IF scoring each - fine
    live at 5Hz, just volume), so it only runs with PGUARD_GOLDEN=1 set.
    Re-run scratch/replay_harness.py after ANY health_monitor.py change
    and update these numbers deliberately - never to make the test pass
    blindly. The actual-counts print below is intentional: on mismatch it
    shows the true numbers without needing another 15-minute run to see
    them (but still re-run to confirm any re-pin)."""

    @unittest.skipUnless(os.environ.get("PGUARD_GOLDEN") == "1",
                         "set PGUARD_GOLDEN=1 to run the full-bag replay")
    def test_event_counts(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent / "scratch"))
        from replay_harness import load_all, replay
        counts, _log, _el = replay(load_all(), speed=1.0)
        by_kind = {}
        for (kind, _signal), v in counts.items():
            by_kind[kind] = by_kind.get(kind, 0) + v
        # Pinned 2026-09-07, speed=1.0, after fault-window baseline
        # suppression landed (that fix alone took statistical 244 -> 33:
        # the fault window plus its 180s contamination tail had been the
        # dominant statistical-event source) plus the unified-pool
        # refinement (liveness pools keep feeding through faults: sensor
        # 11 -> 5). The 6 eliminated sensor_health events were spurious
        # clear/refire flapping of the permanently-dead motor_temperature
        # flags: freezing the unified pool through the 878s fault meant the
        # first post-fault push evicted the whole window (n=1), so is_flat
        # went False on a sensor that never came alive - once around a
        # genuine mid-fault fault=0 blip both controllers report at
        # t~=3357.5s, once at the real clear (4004.6s). Fire-once-and-hold
        # is the correct behavior for a dead sensor. The pre-2026-09 README
        # table is superseded by these numbers. GNSS re-pinned 2026-09-07
        # (17 -> 12) when the gate went set-based ({0,1,5}) with 2s
        # debounce: 44 degraded episodes, 29 flicker-filtered, 12 sustained;
        # failure 24 -> 23 follows (one fewer gnss escalation). Category 9
        # (ml) pinned 2026-09-08 at 5: debounced ml:anomaly events over the
        # full run - consistent with the Phase 3 validation's sustained-run
        # counts (fault-window long event incl. blip/driving-seg splits plus
        # normal-pool transient runs). All seven older keys byte-identical
        # with ML wired, confirming the wiring perturbed nothing.
        # PROVEN-DEAD re-pin, PREDICTED 2026-09-10, pending a
        # PGUARD_GOLDEN=1 confirmation run to confirm:
        # the 4 motor_temperature dead flags move kind sensor_health ->
        # sensor_failure (sentinel match), each escalating immediately
        # (+4 failure, capability supervision). sensor_health keeps only
        # the LF channel_temperature event (5 -> 1). Stride-pool switch is
        # verdict-neutral on 5Hz replay by construction (same span/count
        # semantics, ~every-5th sample), so every other key must read
        # identical - any other movement means the liveness rewrite leaked
        # into judgement and must be investigated, not re-pinned.
        # CONFIRMED 2026-11-25 by a PGUARD_GOLDEN=1 run (906s, OK) - after an
        # investigation: the first confirmation run read sensor_health 4,
        # not 1. The stride was NOT verdict-neutral for the driving-gated
        # channel_temperature: brief 56<->57 / 58-60 degC changes between
        # 1s stride instants were never pooled, so 3 live sensors read flat
        # (LR 2410s, LF 2446s, LF 3212s). Fixed in _check_sensor_liveness
        # (value changes always pooled) + TestLivenessSeesBriefChanges; the
        # predicted dict below then passed unchanged, every other key equal.
        expected = {"statistical": 33, "residual": 20, "hardware_fault": 10,
                    "gnss": 12, "sensor_health": 1, "sensor_failure": 4,
                    "comms": 8, "failure": 27, "ml": 5}
        print("\nGOLDEN actual:", dict(sorted(by_kind.items())), flush=True)
        self.assertEqual(by_kind, expected)


if __name__ == "__main__":
    unittest.main()
