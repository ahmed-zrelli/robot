"""
Live "system health" detection for the dashboard.

Built with no labeled fault data available (unconfirmed whether any exists
- see README). That constraint shapes exactly what this can honestly claim
to do:

1. HARDWARE FAULTS (real classification, for free)
   Decoded directly from the roboteq driver's own status bitmask
   (`fault` field on /left|right_roboteq_driver/status). These are the
   motor controller's own built-in fault detection - OVERHEAT,
   OVERVOLTAGE, UNDERVOLTAGE, SHORT_CIRCUIT, EMERGENCY_STOP, etc. This
   code isn't inventing these categories, it's just surfacing what the
   controller already knows. Edge-triggered directly off the bitmask, no
   debounce - the controller's own fault report shouldn't be delayed.

2. STATISTICAL ANOMALIES (detection only, NOT classification)
   A rolling baseline (mean/std) per numeric signal, over the last
   WINDOW_SECONDS of real time (not a fixed sample count - see below),
   flagged when a new value is a statistical outlier (z-score past a
   threshold) for CONSECUTIVE_REQUIRED samples in a row. This can say
   "this looks unusual compared to what we've recently seen" - it CANNOT
   say what kind of fault it is, because there's no labeled data to have
   learned fault categories from.

3. GNSS FIX DEGRADATION
   Flags when the fix drops below a 3D fix - a direct, documented field
   from the receiver, not a statistical guess. Edge-triggered, no
   debounce - same reasoning as hardware faults.

4. COMMANDED-VS-MEASURED VELOCITY RESIDUAL
   Per motor, the gap between what was commanded and what was actually
   achieved. A persistently large gap is a classic mechanical/control
   health signal (e.g. wheel slip, stall, load). Still just a statistical
   flag on the *gap*, not a diagnosis of the cause. Same time-window +
   debounce treatment as (2).

Explicitly NOT implemented: predicting failure before it happens
("anticipate"). That needs historical examples of what conditions preceded
a real past failure, which we don't have. Building that without evidence
would just be a confident-looking guess, not a real signal.

WHY A TIME WINDOW, NOT A SAMPLE COUNT
Different topics publish at very different rates (e.g. battery ~1Hz vs.
motor feedback ~5Hz in the reference bag - real robot rates unconfirmed).
A fixed sample count means "recent" silently means a different amount of
real time per signal. A time window (WINDOW_SECONDS) makes "recent" mean
the same thing for every signal. MAX_SAMPLES is a separate safety cap -
protects memory/CPU if a topic publishes much faster than expected (real
live-mode rates aren't confirmed yet); it does not redefine "recent", it
only bounds the cost of computing it.

WHY DEBOUNCE (CONSECUTIVE_REQUIRED)
Without it, a single sample that crosses the z-score threshold fires
`new`, and the very next sample back under it fires `cleared` - this was
confirmed live (flood of new/cleared pairs within seconds of connecting).
Requiring N consecutive anomalous samples before firing `new`, and N
consecutive normal samples before firing `cleared`, kills single-sample
flapping. Only applied to the statistical/residual checks - the hardware
fault bitmask and GNSS fix checks are direct controller/receiver reports,
not noisy derived statistics, so debouncing them would only add
unjustified delay to a real, discrete state change.

WHY WARM_UP EXISTS (see also main.py, db.py)
A fresh HealthMonitor() is still created per connection - in-memory state
is not carried across reconnects. But the historical telemetry itself is
durable (SQLite, all sessions, see db.py). warm_up() lets a caller
pre-fill each RollingStat from that history before live samples start
arriving, so the baseline isn't judging off a near-empty window right
after connecting (which was producing distorted z-scores, e.g. z=57).
Only the direct per-field statistical baselines are warmed up this way -
NOT the commanded-vs-measured residual baseline, since reconstructing
historical residuals requires replaying matched cmd/feedback pairs in
order, which is meaningfully more complex and out of scope for this
pass; residual detection still starts cold on every reconnect.

All state here is per-connection (one HealthMonitor instance per WebSocket
session) and resets when the connection closes, except for whatever
warm_up() explicitly pre-seeds from durable history before streaming
starts - consistent with the rest of the project's "no persistence in V0
beyond the DB" scope.
"""
import math
from collections import deque, defaultdict

from enums import FAULT_BITS

Z_THRESHOLD = 3.5             # |z-score| past this counts as anomalous
MIN_BASELINE_SAMPLES = 25     # don't judge a signal until we've seen this many samples
WINDOW_SECONDS = 180.0        # rolling baseline covers the last 3 minutes of real time
MAX_SAMPLES = 900             # safety cap on samples held per signal, independent of time
CONSECUTIVE_REQUIRED = 3      # samples in a row needed before flipping new/cleared

# Numeric fields worth statistically monitoring, per topic. Deliberately a
# short, explainable list - not "monitor every float in every message".
MONITORED_FIELDS = {
    "/battery/state": ["voltage", "current", "temperature"],
    "/left/front/feedback": ["motor_current", "channel_temperature", "supply_voltage"],
    "/left/rear/feedback": ["motor_current", "channel_temperature", "supply_voltage"],
    "/right/front/feedback": ["motor_current", "channel_temperature", "supply_voltage"],
    "/right/rear/feedback": ["motor_current", "channel_temperature", "supply_voltage"],
}

FEEDBACK_MOTOR = {
    "/left/front/feedback": "left_front",
    "/left/rear/feedback": "left_rear",
    "/right/front/feedback": "right_front",
    "/right/rear/feedback": "right_rear",
}
CMD_MOTOR = {
    "/left/front/cmd": "left_front",
    "/left/rear/cmd": "left_rear",
    "/right/front/cmd": "right_front",
    "/right/rear/cmd": "right_rear",
}
STATUS_SIDE = {
    "/left_roboteq_driver/status": "left",
    "/right_roboteq_driver/status": "right",
}


class RollingStat:
    """Time-windowed mean/std, recomputed from the window each call.
    Keeps (t, value) pairs; on each push, evicts anything older than
    WINDOW_SECONDS relative to the newest timestamp seen, and separately
    caps total length at MAX_SAMPLES as a safety net against unexpectedly
    high publish rates. Simple and easy to reason about; cheap at these
    data rates (see MAX_SAMPLES)."""

    def __init__(self, window_seconds=WINDOW_SECONDS, max_samples=MAX_SAMPLES):
        self.window_seconds = window_seconds
        self.samples = deque(maxlen=max_samples)  # each item: (t, value)

    def push(self, t, x):
        self.samples.append((t, x))
        cutoff = t - self.window_seconds
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    def zscore(self, x):
        n = len(self.samples)
        if n < MIN_BASELINE_SAMPLES:
            return None
        values = [v for _, v in self.samples]
        mean = sum(values) / n
        var = sum((v - mean) ** 2 for v in values) / n
        std = math.sqrt(var)
        if std < 1e-9:
            return 0.0
        return (x - mean) / std


class HealthMonitor:
    def __init__(self):
        self._stats = defaultdict(RollingStat)
        self._active = {}              # event key -> event dict, currently active only
        self._consec_anom = defaultdict(int)    # key -> consecutive anomalous samples
        self._consec_normal = defaultdict(int)  # key -> consecutive normal samples
        self._last_fault_byte = {}     # side -> last seen fault byte
        self._last_cmd = {}            # motor -> last commanded_velocity seen

    def warm_up(self, telemetry_by_topic):
        """Pre-fill statistical baselines from historical data (e.g. loaded
        from the DB) before live samples start arriving. `telemetry_by_topic`
        is {topic: [(t, data), ...]}, oldest first, t as unix seconds so
        durations compare sanely against WINDOW_SECONDS. Only seeds the
        direct per-field stats used by _check_statistical_fields - does NOT
        emit any events (historical data should never surface as a fresh
        health event), and does NOT warm up the residual check (see
        module docstring)."""
        for topic, rows in telemetry_by_topic.items():
            fields = MONITORED_FIELDS.get(topic)
            if not fields:
                continue
            for t, data in rows:
                for field in fields:
                    val = data.get(field)
                    if isinstance(val, (int, float)) and math.isfinite(val):
                        self._stats[f"stat:{topic}:{field}"].push(t, val)

    def ingest(self, topic, data, t):
        """Feed one telemetry message in. `t` MUST be wall-clock time
        (time.time()), NOT the stream's own session-relative timestamp -
        bag mode's replay-relative t and live mode's connection-relative
        t both restart near 0 every session, which would corrupt the
        rolling window's time-based eviction (and disagree with the
        wall-clock timestamps warm_up() seeds it with). Returns a list of
        edge-triggered events (only fires when something starts or stops
        being flagged, not on every sample) - each has state 'new' or
        'cleared'."""
        events = []
        events += self._check_hardware_fault(topic, data)
        events += self._check_statistical_fields(topic, data, t)
        events += self._check_velocity_residual(topic, data, t)
        events += self._check_gnss_fix(topic, data)
        return events

    def _emit_new(self, key, kind, severity, signal, message):
        ev = {"key": key, "kind": kind, "severity": severity, "signal": signal, "message": message}
        self._active[key] = ev
        return {**ev, "state": "new"}

    def _emit_cleared(self, key):
        old = self._active.pop(key)
        return {**old, "state": "cleared"}

    def _debounced_transition(self, key, is_anomalous):
        """Shared debounce bookkeeping. Returns 'fire_new', 'fire_cleared',
        or None. Requires CONSECUTIVE_REQUIRED samples in a row in the new
        direction before actually flipping state."""
        if is_anomalous:
            self._consec_anom[key] += 1
            self._consec_normal[key] = 0
        else:
            self._consec_normal[key] += 1
            self._consec_anom[key] = 0

        was_active = key in self._active
        if not was_active and self._consec_anom[key] >= CONSECUTIVE_REQUIRED:
            return "fire_new"
        if was_active and self._consec_normal[key] >= CONSECUTIVE_REQUIRED:
            return "fire_cleared"
        return None

    def _check_hardware_fault(self, topic, data):
        side = STATUS_SIDE.get(topic)
        if side is None:
            return []
        fault_byte = data.get("fault", 0) or 0
        prev = self._last_fault_byte.get(side, 0)
        self._last_fault_byte[side] = fault_byte
        if fault_byte == prev:
            return []

        key = f"fault:{side}"
        if fault_byte:
            names = [name for bit, name in FAULT_BITS.items() if fault_byte & bit]
            msg = f"Controller fault: {', '.join(names) if names else fault_byte}"
            return [self._emit_new(key, "hardware_fault", "critical", f"{side} motor driver", msg)]
        elif key in self._active:
            return [self._emit_cleared(key)]
        return []

    def _check_statistical_fields(self, topic, data, t):
        out = []
        for field in MONITORED_FIELDS.get(topic, []):
            val = data.get(field)
            if not isinstance(val, (int, float)) or not math.isfinite(val):
                continue
            key = f"stat:{topic}:{field}"
            stat = self._stats[key]
            z = stat.zscore(val)
            stat.push(t, val)
            if z is None:
                continue
            is_anomalous = abs(z) > Z_THRESHOLD
            transition = self._debounced_transition(key, is_anomalous)
            if transition == "fire_new":
                label = topic.strip("/").replace("/", " ")
                msg = f"{field} unusual: {val:.2f} (z={z:.1f} vs recent baseline)"
                out.append(self._emit_new(key, "statistical", "warning", f"{label} — {field}", msg))
            elif transition == "fire_cleared":
                out.append(self._emit_cleared(key))
        return out

    def _check_velocity_residual(self, topic, data, t):
        if topic in CMD_MOTOR:
            cv = data.get("commanded_velocity")
            if isinstance(cv, (int, float)):
                self._last_cmd[CMD_MOTOR[topic]] = cv
            return []

        motor = FEEDBACK_MOTOR.get(topic)
        if motor is None:
            return []
        mv = data.get("measured_velocity")
        cv = self._last_cmd.get(motor)
        if not isinstance(mv, (int, float)) or not isinstance(cv, (int, float)):
            return []

        residual = abs(mv - cv)
        key = f"residual:{motor}"
        stat = self._stats[key]
        z = stat.zscore(residual)
        stat.push(t, residual)
        if z is None:
            return []

        is_anomalous = z > Z_THRESHOLD  # one-sided: only unusually LARGE gaps matter
        transition = self._debounced_transition(key, is_anomalous)
        if transition == "fire_new":
            msg = f"commanded vs measured velocity gap unusual: {residual:.2f} rad/s (z={z:.1f})"
            return [self._emit_new(key, "residual", "warning", f"{motor} motor", msg)]
        elif transition == "fire_cleared":
            return [self._emit_cleared(key)]
        return []

    def _check_gnss_fix(self, topic, data):
        if topic != "/ublox/gnss_status":
            return []
        fix_type = data.get("fixType")
        if fix_type is None:
            return []
        key = "gnss:fix"
        degraded = fix_type < 3
        was_active = key in self._active
        if degraded and not was_active:
            msg = f"fix quality degraded (fixType={fix_type}, not 3D)"
            return [self._emit_new(key, "gnss", "warning", "GPS / GNSS", msg)]
        elif not degraded and was_active:
            return [self._emit_cleared(key)]
        return []
