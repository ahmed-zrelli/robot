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
   threshold) for DEBOUNCE_SECONDS continuously. This can say
   "this looks unusual compared to what we've recently seen" - it CANNOT
   say what kind of fault it is, because there's no labeled data to have
   learned fault categories from.

3. GNSS FIX DEGRADATION
   Flags when the receiver reports no usable GNSS position
   (GNSS_DEGRADED_FIX = {NO_FIX, DEAD_RECKONING, TIME_ONLY}) - a direct,
   documented field from the receiver, not a statistical guess.
   Debounced through the shared 2s persistence transition (the report
   itself flickers: 29/44 degraded episodes under 2s in the reference
   bag), unlike hardware faults which stay edge-triggered.

4. COMMANDED-VS-MEASURED VELOCITY RESIDUAL
   Per motor, the gap between what was commanded and what was actually
   achieved. A persistently large gap is a classic mechanical/control
   health signal (e.g. wheel slip, stall, load). Still just a statistical
   flag on the *gap*, not a diagnosis of the cause. Same time-window +
   debounce treatment as (2).

5. MANEUVER SUPPRESSION (added after replaying this on the first real,
   actively-driven bag)
   The statistical and residual checks above compare against a rolling
   baseline that mixes idle/steady-state samples with maneuver samples.
   On a mostly-idle bag that's fine. On a bag with real turns, it isn't:
   confirmed against this bag's raw commanded_velocity/current/voltage
   data that a single, completely ordinary turn (one side's commanded
   velocity drops, the other's rises - normal differential-drive
   steering) makes current/voltage jump on all four motors and the
   battery simultaneously, and that jump is large enough and fast enough
   to blow past Z_THRESHOLD on every one of those fields at once, every
   time the robot turns. That's not a fault, it's physics - the z-score
   check has no way to distinguish "changed because the robot is turning"
   from "changed because something's wrong" unless it's told.
   Fix: watch commanded_velocity on each motor's /cmd topic for a step
   bigger than MANEUVER_CMD_DELTA_THRESHOLD. When one happens, current and
   voltage fields (MANEUVER_SENSITIVE_FIELDS - NOT channel_temperature,
   NOT hardware faults, NOT GNSS) are excused from statistical/residual
   judgement for the next MANEUVER_SETTLE_SECONDS, and the window keeps
   sliding forward as long as the robot keeps maneuvering (continuous
   steering adjustments during a turn each reset the timer), so a whole
   turn is covered, not just its first instant. Samples during suppression
   are dropped entirely - not fed into the baseline either - so the
   baseline keeps representing steady-state operation, which is what
   actually needs to be sensitive to real anomalies once the maneuver
   ends.
   Known, accepted trade-off: a genuine current/voltage/tracking fault
   that starts in the same few seconds as a maneuver will be masked until
   MANEUVER_SETTLE_SECONDS after the maneuver ends. Hardware faults (the
   controller's own bitmask) and GNSS degradation are untouched by this -
   they're direct reports, not statistics, so they were never the problem.

5b. IDLE / DRIVING BASELINE SPLIT (added after replaying the fixes above -
   see _check_statistical_fields)
   MANEUVER_SETTLE_SECONDS only excuses the few seconds right after a
   commanded step. But sustained driving keeps current/voltage elevated
   for as long as the robot keeps moving, not just at the instant a
   command changes - a single mixed baseline (idle + driving samples
   together) ends up too tight while driving and too loose while idle,
   whichever state currently dominates the window. This was the largest
   remaining source of statistical events after fixes 5 and 6 (mostly
   supply_voltage). Fix: each monitored field now has two RollingStat
   pools, one fed only while the robot is driving (any motor's last
   commanded_velocity nonzero - _is_driving()) and one fed only while
   idle; z-score judgement uses whichever pool matches the current state.
   The original unsuffixed pool per field is still maintained and fed
   unconditionally, because sensor-liveness detection (6) needs to see
   the whole window regardless of state. The debounce/active-event key is
   also unsuffixed, so a field transitioning between idle and driving
   mid-anomaly doesn't reset its consecutive-sample counter or open a
   second event for the same thing.

6. SENSOR LIVENESS (suspect vs proven-dead - different claims, different fates)
   A monitored field whose value is bit-identical across 90s+ of
   stride-sampled history (see LIVENESS_STRIDE_S - stride sampling keeps
   the span requirement rate-invariant; judging the capped RollingStat
   pools broke above ~10Hz live) is flagged. This is a distinct category
   from everything above: a flat reading doesn't mean the robot is fine OR
   broken, it means this code can't currently tell - which is itself worth
   surfacing, because it's a monitoring blind spot that could be hiding
   a real problem. Confirmed real cases in this bag: /battery/state
   `temperature` never moves at all across the full 212-minute run
   (zero non-zero deltas) - this check catches exactly that.
   Flat splits two ways (see PROVEN_DEAD_SENSORS): a value matching a
   registered physically-impossible sentinel (motor_temperature -4.1754)
   fires "sensor_failure" (critical, capability "supervision") - proven,
   no healthy sensor can read that. Anything else fires "sensor_health"
   (warning, no capability) - suspicion only, and stays that way no
   matter how long it persists.

7. COMMS / TOPIC LIVENESS
   Tracks the interval between consecutive messages on every topic seen
   so far (a running per-topic median - not a hardcoded expected rate,
   since different topics publish at genuinely different real rates and
   that shouldn't need to be told to the code by hand). If a topic goes
   quiet for longer than max(STALE_FLOOR_SECONDS, STALE_MULTIPLIER x its
   own recent median interval), it's flagged. This can only be evaluated
   when *some* message arrives (there's no background timer here - this
   runs inside ingest()), so total silence across every topic at once
   won't be caught until the next message on any topic arrives; a single
   topic going quiet while others keep flowing (the more likely real
   failure mode, e.g. a type-mismatch drop like the /battery/state
   rosbridge issue from an earlier session) is caught correctly.

8. FAILURE ESCALATION (turns the agreed taxonomy into actual state)
   Anomaly/fault/degradation (1,2,3,4) are all still just "something
   looks off" at various confidence levels - none of them by themselves
   say the robot can no longer do its job. This layer adds that: if one
   of those events stays continuously active past a capability-specific
   ESCALATION_SECONDS window, a second, separate    `failure` event fires,
   tagged with which core capability (mobility / localization / power /
   supervision) is impaired and for how long. The original event is untouched and
   keeps firing/clearing on its own - `failure` is additive, not a
   replacement, so nothing about the existing signal is lost.
   Hardware faults escalate immediately (0s) - the controller has
   already confirmed it, waiting would only add false delay. Proven-dead
   sensors escalate immediately too (0s) - the 90s flat span required for
   detection IS the persistence proof. Everything else waits, because
   they're statistical/derived and momentary blips shouldn't read as
   "the robot has failed."
   Merely-suspect sensor_health and comms events are explicitly excluded
   from escalation - a flat reading or a quiet topic is a monitoring gap,
   not proof the robot itself can't do its job. Capabilities now include
   "supervision" (safety supervision impaired by a blind sensor) alongside
   mobility/localization/power - stated as exactly that, never borrowed.
   NOT YET DEFINED (needs Ahmed's input, not guessed): what counts as a
   "docked but not charging" power failure (semantics of /charging and
   /coil_outputs aren't confirmed), and whether GNSS accuracy
   degradation via diffMode/carrierStatus/rtk_correction (as opposed to
   the existing fixType<3 check) belongs here. Both left out rather than
   invented.

9. FAULT-WINDOW BASELINE SUPPRESSION
   While a controller reports a hardware fault, its telemetry IS the
   fault, not normal operation to learn from (reference bag: motor
   supply_voltage collapsed for the full 878s UNDERVOLTAGE window, motor
   current variance x19). Split-pool feeding and judgement for a faulted
   side pause while its fault bit is set - same pattern as maneuver
   suppression (item 5): fault samples never teach the judgement baseline
   what "normal" looks like. The unified pool keeps feeding throughout (it
   is never z-scored), so dead-sensor detection stays live through a
   fault. The battery (power-system-wide) pauses while ANY side is
   faulted. Without this, the baseline learns the fault as normal and
   stays contaminated for a full WINDOW_SECONDS after the fault clears,
   flagging genuine recovery data as anomalous. Fail-open: suppression
   only starts once a status topic has actually reported fault != 0.

10. ML FUSION - CATEGORY 9 (Phase 3, see _check_ml)
    One fused `ml:anomaly` event from ml/infer.py (Isolation Forest OR
    Autoencoder over 13 stationary features, per-state thresholds, validated
    online: 1.09% FPR, 0.56 events/h, 0.1s fault delay). 2s persistence via
    the shared debounce; capability=None so it never escalates to failure
    (no labeled precedent for an ML escalation window - Phase 4 question).
    Fail-soft: absent artifacts mean ML stays off and everything else is
    untouched. Deliberately NOT suppressed while a hardware fault is active
    (unlike item 9's baselines): the model is frozen, so fault samples are
    signal, not contamination.

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

WHY DEBOUNCE (DEBOUNCE_SECONDS)
Without it, a single sample that crosses the z-score threshold fires
`new`, and the very next sample back under it fires `cleared` - this was
confirmed live (flood of new/cleared pairs within seconds of connecting).
Requiring continuous real time in the new direction before flipping state
kills single-sample flapping. Originally a 3-sample count; replaced with
wall-clock time because a sample count is rate-dependent - 3 samples at
50Hz is 60ms of protection, at 1Hz it's 3s - so the same constant meant
wildly different real protection per signal. Time is rate-invariant.
Final value swept against the full reference bag (scratch/sweep.py,
scratch/sweep_results.csv): 1s left heavy flapping, 3-5s started eating
genuine sustained residual faults; 2s kept all of them while removing the
flap flood. Cost, measured: a real anomaly's `new` now fires a median
~2.3s after its first crossing sample (worst observed ~54s when a signal
flaps around threshold - the run timer resets each time it dips normal,
which is the point). Hardware fault bitmask and GNSS fix checks remain
edge-triggered with no debounce - direct controller/receiver reports,
not noisy derived statistics, so debouncing them would only add
unjustified delay to a real, discrete state change.

WHY WARM_UP EXISTS (see also main.py, db.py)
A fresh HealthMonitor() is still created per connection - in-memory state
is not carried across reconnects. But the historical telemetry itself is
durable (SQLite, all sessions, see db.py). warm_up() lets a caller pre-fill each RollingStat from that history before
live samples start arriving, so the baseline isn't judging off a near-empty
window right after connecting (which was producing distorted z-scores, e.g.
z=57). Both the direct per-field statistical baselines AND the
commanded-vs-measured residual baselines are warmed this way (residuals via
_warm_up_residuals()'s matched cmd/feedback merge walk, which also seeds
_last_cmd so _is_driving() starts truthful). Still cold after warm_up():
the idle/driving state-split pools (attributing every historical sample to
its drive-state stays deferred) and maneuver-suppression history
(_maneuver_until restarts at 0, fail-open by design).

All state here is per-connection (one HealthMonitor instance per WebSocket
session) and resets when the connection closes, except for whatever
warm_up() explicitly pre-seeds from durable history before streaming
starts - consistent with the rest of the project's "no persistence in V0
beyond the DB" scope.
"""
import math
import sys
from collections import deque, defaultdict
from pathlib import Path

from enums import FAULT_BITS, FIX_TYPES

# Phase 3 ML fusion (ml/infer.py MLScorer): optional. Import is cheap
# (numpy only - onnxruntime/sklearn load lazily inside MLScorer()); if
# anything about it fails (no ml/ on the path, no artifacts built), the
# monitor runs exactly as before with ML silently off. Deterministic
# monitoring must never depend on ML files being present.
try:
    _ML_PARENT = str(Path(__file__).resolve().parent.parent)
    if _ML_PARENT not in sys.path:
        sys.path.insert(0, _ML_PARENT)
    from ml.infer import MLScorer, ML_TOPICS
except Exception:
    MLScorer, ML_TOPICS = None, frozenset()

Z_THRESHOLD = 3.5             # |z-score| past this counts as anomalous
MIN_BASELINE_SAMPLES = 25     # don't judge a signal until we've seen this many samples
WINDOW_SECONDS = 180.0        # rolling baseline covers the last 3 minutes of real time
MAX_SAMPLES = 900             # safety cap on samples held per signal, independent of time

# TIME-BASED DEBOUNCE (replaces the old CONSECUTIVE_REQUIRED=3 sample count -
# see docstring "WHY DEBOUNCE"). A statistical/residual anomaly must persist
# continuously for this much real time before `new` fires, and its absence
# must persist as long before `cleared` fires. Sample-count debounce was
# rate-dependent: 3 samples at 50Hz is 60ms (no flap protection at all) while
# 3 samples at 1Hz is 3s - the same constant meant different amounts of real
# protection per signal. Wall-clock time is rate-invariant. Final value
# picked empirically by sweeping against the full reference bag (see
# scratch/replay_harness.py) - not hand-picked.
DEBOUNCE_SECONDS = 2.0

# QUANTIZATION FLOOR STABILIZATION (see RollingStat): the resolution estimate
# is computed over a long, non-time-evicted history rather than just the
# current 180s window (which is sometimes entirely flat-at-one-value or
# noisy, making the estimate jump around), and a running max keeps it from
# transiently shrinking. QUANT_HISTORY caps memory; QUANT_RECOMPUTE_EVERY
# caps recompute cost (median-of-gaps is O(n log n)) - a pure perf cap, not
# a detection threshold.
QUANT_HISTORY = 5000
QUANT_RECOMPUTE_EVERY = 200

# MANEUVER SUPPRESSION (see module docstring addendum below for the full
# reasoning) - a sharp change in a motor's commanded_velocity means current
# and voltage draw are EXPECTED to shift on every motor and on the battery,
# because of real physics (differential drive: one side slows, the other
# speeds up, load changes on all four channels at once). That's not
# something a magnitude threshold picked from this run's numbers should be
# treated as universal - MANEUVER_CMD_DELTA_THRESHOLD is derived from the
# actual distribution of step-to-step commanded_velocity changes in the
# reference bag (single-step deltas are 0 nearly all the time - the
# controller only republishes on change - so the ~99th percentile of
# nonzero deltas, ~6-7 units, cleanly separates real maneuvers from noise;
# 15.0 sits comfortably above that with margin) - it will need re-checking
# against real robot data once available, same as every other threshold in
# this file.
MANEUVER_CMD_DELTA_THRESHOLD = 15.0   # commanded_velocity step judged as "a real maneuver"
MANEUVER_SETTLE_SECONDS = 5.0         # how long current/voltage stay excused after the last such step

DEAD_SENSOR_MIN_SPAN_SECONDS = 90.0   # a field frozen across this much real time is suspect

# Stride-sampled liveness history: dead-sensor judgement needs a 90s time
# span, but RollingStat pools are capped at MAX_SAMPLES - at the live
# ~50Hz native rate 900 samples cover ~18s, so is_flat() on those pools can
# NEVER go true live (measured: warm_up seeds fired motor_temperature dead
# flags at session start, then 900-cap eviction collapsed the span and they
# spuriously cleared ~17s later, never to refire). Liveness therefore keeps
# its own pool per field: one sample per LIVENESS_STRIDE_S plus time
# eviction, so the span requirement is rate-invariant by construction.
# Same MIN span/count semantics as before, just on guaranteed time cover.
LIVENESS_STRIDE_S = 1.0
LIVENESS_HISTORY = 400  # bounds memory; with stride + time eviction below
LIVENESS_WINDOW_S = 200.0  # time-eviction horizon for liveness pools

# Fields where "flat" is the physically-expected state while idle (no
# load -> current settles, thermal mass -> temperature settles), so
# dead-sensor judgement only makes sense while the robot is under load.
# channel_temperature confirmed directly against this bag (see
# _check_sensor_liveness); motor_current added on the same evidence after
# this bag's idle tail produced the identical flat-for-90s+ pattern on it.
DRIVING_GATED_LIVENESS_FIELDS = {"channel_temperature", "motor_current"}

# Only fields with evidence that a flat value represents a sensor/reporting
# failure belong in generic flatness detection. motor_temperature is the
# confirmed case in the reference bag: every motor reports the impossible
# thermal-sensor sentinel -4.1754C with zero variance. Do NOT put generic
# command/status/GNSS fields here: a topic can legitimately repeat a value,
# publish only on change, or report a valid zero. Topic silence is handled as
# communications/availability, not as a dead sensor.
SENSOR_LIVENESS_FIELDS = {"motor_temperature"}

# Proven-dead sensor sentinels: (topic, field) -> (value, tolerance, reason).
# A flat reading separates "suspect" (could be physics: idle settling,
# quantization, publish-on-change) from "proven" (no healthy sensor can
# produce this value, ever). Registry entries need a physically-impossible
# constant verified across a full recording - the bar is deliberately "prove
# a healthy sensor cannot read this", not "it looked flat for a while".
# Seed entry: all four motors report -4.1754C with std=0.0 across the
# full 212-minute reference bag AND across live sessions (9,406/9,406 live
# rows bit-identical): the thermal sensor is unwired, and the firmware
# reports its sentinel. Tolerance covers float32-vs-float64 representation
# (measured literal gap ~2e-8); the window is already proven bit-identical
# by is_flat semantics before this is consulted, so the tolerance only
# bridges representation, never judgement.
# Proven-dead findings escalate (capability "supervision", see
# ESCALATION_SECONDS): a blind thermal sensor impairs safety supervision,
# and that is stated as exactly that - never borrowed mobility/power tags.
PROVEN_DEAD_SENSORS = {
    ("/left/front/feedback", "motor_temperature"):
        (-4.1754, 1e-6, "unwired thermal-sensor sentinel"),
    ("/left/rear/feedback", "motor_temperature"):
        (-4.1754, 1e-6, "unwired thermal-sensor sentinel"),
    ("/right/front/feedback", "motor_temperature"):
        (-4.1754, 1e-6, "unwired thermal-sensor sentinel"),
    ("/right/rear/feedback", "motor_temperature"):
        (-4.1754, 1e-6, "unwired thermal-sensor sentinel"),
}
STALE_FLOOR_SECONDS = 5.0             # never call a topic stale faster than this, regardless of its rate
STALE_MULTIPLIER = 5.0                # flag a topic once it's this many times its own median interval overdue

# COMMS STALENESS - percentile threshold (see _check_comms_stale). Publish-
# on-change topics (/cmd) legitimately go quiet for long stretches while the
# robot holds a steady command; a median-based multiplier flags that silence
# even though it's expected. Fix: compare the current quiet gap against a
# high percentile of the topic's OWN historical gap distribution instead of
# its median. STALE_PERCENTILE is swept empirically against the reference
# bag; STALE_SCOPE selects whether this smarter threshold applies to every
# topic or only known publish-on-change ones (the *cmd suffixes).
STALE_PERCENTILE = 99.0
STALE_SCOPE = "all"   # "all" | "cmd"
# ROS / rosbridge infrastructure topics that publish only when something
# happens (a node logs a line, a client connects). Silence there is the
# normal state, not a comms failure, and none of them carries robot state.
# Found on the first live run against the Docker virtual robot: /rosout and
# /rosout_agg raised critical comms alerts within seconds of connecting.
# Absent from the reference bag, so the golden counts are unaffected.
COMMS_EXEMPT_TOPICS = frozenset({
    "/rosout", "/rosout_agg", "/client_count", "/connected_clients",
})

# Fields known-absent for the reference hardware/message variant - excluded
# from field_coverage_warnings() so that log line keeps meaning "a NEW
# absence worth investigating", not a known fact re-announced every
# session. ("/battery/state", "temperature"): this bag's older
# sensor_msgs/BatteryState variant (13 fields, has `location`/
# `serial_number`) predates the `temperature` field entirely - confirmed
# absent in all 12,758 messages, not a sensor failure.
KNOWN_ABSENT_FIELDS = {("/battery/state", "temperature")}

# GNSS fix gate (see _check_gnss_fix): fix types in this set read as
# degraded. A SET, not an ordinal threshold, on purpose: the enum is not
# ordered by badness (TIME_ONLY=5 is worse than 2D=2 but compares greater,
# so the old `fix < 3` rule silently treated "no position at all" as
# healthy - a real bug, 259 such samples in the reference bag). Measured
# distribution (49,725 samples): 2 (2D) 71.2%, 4 (GNSS+DR) 24.4%, 1 (DR)
# 3.6%, 5 (TIME_ONLY) 0.5%, 0 (NO_FIX) 0.3%; 3D never occurs on this
# hardware. The useful line is "does the receiver have any GNSS-derived
# position": 2/3/4 do (varying precision - precision itself is a future
# carrierStatus/diffMode question, not this check), 0/1/5 don't. 4
# (GNSS+DR) counts as healthy: still GNSS-anchored with bounded error,
# and at 24.4% of the run it is plainly this environment's normal.
# Debounced (2s persistence via the shared transition helper), NOT
# edge-triggered: measured 44 degraded episodes under this rule with
# median duration 0.9s, 29/44 under 2s - the receiver report itself
# flickers, so edge-triggering would flap ~44x per run. 2s delay against
# a 30s escalation window is negligible.
GNSS_DEGRADED_FIX = frozenset({0, 1, 5})

# How long an anomaly/fault/degradation must stay continuously active
# before it's escalated to a `failure` (capability-impaired) event. See
# docstring item 8. hardware_fault escalates immediately - it's already a
# confirmed controller report, not a derived statistic.
# sensor_failure (proven-dead sentinel, see PROVEN_DEAD_SENSORS) also
# escalates immediately: the 90s flat span required for detection IS the
# persistence proof, and a blind safety sensor impairs the supervision
# capability. Merely-suspect sensor_health (capability None) never does.
ESCALATION_SECONDS = {
    "hardware_fault": 0.0,
    "sensor_failure": 0.0,
    "residual": 15.0,
    "gnss": 30.0,
    "statistical": 20.0,
}

# Which core capability (see docstring item 8) each statistical topic's
# fields belong to, for failure-escalation tagging.
STATISTICAL_CAPABILITY = {
    "/battery/state": "power",
    "/left/front/feedback": "mobility",
    "/left/rear/feedback": "mobility",
    "/right/front/feedback": "mobility",
    "/right/rear/feedback": "mobility",
}

# Numeric fields worth statistically monitoring, per topic. Deliberately a
# short, explainable list - not "monitor every float in every message".
MONITORED_FIELDS = {
    "/battery/state": ["voltage", "current", "temperature"],
    "/left/front/feedback": ["motor_current", "channel_temperature", "supply_voltage", "motor_temperature"],
    "/left/rear/feedback": ["motor_current", "channel_temperature", "supply_voltage", "motor_temperature"],
    "/right/front/feedback": ["motor_current", "channel_temperature", "supply_voltage", "motor_temperature"],
    "/right/rear/feedback": ["motor_current", "channel_temperature", "supply_voltage", "motor_temperature"],
}

# Subset of MONITORED_FIELDS that load/current physically couples to
# commanded_velocity - confirmed against this bag (t~63s turn: left/front
# cmd -87.5->-37.4, battery current -6.5A->-16.9A, motor_current up to
# 30-40A, all in the same ~10s window while the turn was in progress).
# channel_temperature is deliberately NOT in here: thermal mass means it
# moves slowly, a real anomaly there isn't a one-maneuver transient, so it
# should keep being judged on the full baseline.
MANEUVER_SENSITIVE_FIELDS = {
    "/battery/state": {"voltage", "current"},
    "/left/front/feedback": {"motor_current", "supply_voltage"},
    "/left/rear/feedback": {"motor_current", "supply_voltage"},
    "/right/front/feedback": {"motor_current", "supply_voltage"},
    "/right/rear/feedback": {"motor_current", "supply_voltage"},
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
    data rates (see MAX_SAMPLES).

    QUANTIZATION FLOOR (confirmed against real driven-robot data): several
    monitored fields (e.g. supply_voltage at 0.1V steps, channel_temperature
    at 1 degree steps) sit essentially flat for long stretches, so their
    true variance over a window can be a tiny fraction of one quantization
    step. A single ordinary step then produces a huge z-score - not because
    anything unusual happened, but because the sample std was computed off
    a window that happened to contain no variation at all. This isn't
    fixed with a hand-picked per-field constant (this file explicitly
    avoids inventing thresholds it can't justify, and different sensors
    have different resolutions the code shouldn't have to be told about
    one-by-one). Instead: estimate each field's own quantization step
    directly from its recent samples (the median nonzero gap between
    consecutive readings in the window), and apply Sheppard's correction -
    the standard result that a value rounded to resolution q has variance
    q^2/12 - as a floor under the measured variance before taking zscore.
    A window that's flat purely because of quantization now reads its true
    noise floor instead of ~0, and z-scores stop being inflated by it. A
    field with genuine continuous variation (motor_current under load,
    etc.) is unaffected, since its measured variance already exceeds the
    quantization floor."""

    def __init__(self, window_seconds=WINDOW_SECONDS, max_samples=MAX_SAMPLES):
        self.window_seconds = window_seconds
        self.samples = deque(maxlen=max_samples)  # each item: (t, value)
        # Long, non-time-evicted history for the quantization-resolution
        # estimate (see _quantization_floor_var). Kept separately from
        # `samples` because the window evicts by time and can end up holding
        # a single flat value - useless for estimating resolution.
        self._quant_history = deque(maxlen=QUANT_HISTORY)
        self._quant_floor_var = 0.0     # running max of the estimate (never shrinks back down)
        self._quant_since_recompute = 0

    def push(self, t, x):
        self.samples.append((t, x))
        cutoff = t - self.window_seconds
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        self._quant_history.append(x)
        self._quant_since_recompute += 1
        if self._quant_since_recompute >= QUANT_RECOMPUTE_EVERY:
            est = self._quantization_floor_var(list(self._quant_history))
            if est > self._quant_floor_var:
                self._quant_floor_var = est  # rolling max: resolution estimate never shrinks transiently
            self._quant_since_recompute = 0

    def _quantization_floor_var(self, values):
        gaps = sorted(abs(values[i] - values[i - 1]) for i in range(1, len(values)))
        gaps = [g for g in gaps if g > 1e-9]
        if not gaps:
            return 0.0
        resolution = gaps[len(gaps) // 2]  # median nonzero step = this field's apparent resolution
        return (resolution ** 2) / 12.0    # Sheppard's correction for rounding/quantization noise

    def zscore(self, x):
        n = len(self.samples)
        if n < MIN_BASELINE_SAMPLES:
            return None
        values = [v for _, v in self.samples]
        mean = sum(values) / n
        var = sum((v - mean) ** 2 for v in values) / n
        var = max(var, self._quant_floor_var)
        std = math.sqrt(var)
        if std < 1e-9:
            return 0.0
        return (x - mean) / std

    def is_flat(self, min_samples, min_span_seconds):
        """True if every value currently in the window is bit-identical
        AND the window covers at least min_span_seconds of real time (so
        a handful of samples that happen to arrive in a tight burst right
        after connecting doesn't look like a dead sensor)."""
        n = len(self.samples)
        if n < min_samples:
            return False
        if self.samples[-1][0] - self.samples[0][0] < min_span_seconds:
            return False
        first_val = self.samples[0][1]
        return all(v == first_val for _, v in self.samples)


class HealthMonitor:
    def __init__(self):
        self._stats = defaultdict(RollingStat)
        self._active = {}              # event key -> event dict, currently active only
        self._anom_since = defaultdict(lambda: None)   # key -> t anomaly run started
        self._norm_since = defaultdict(lambda: None)   # key -> t normal run started
        self._last_fault_byte = {}     # side -> last seen fault byte
        self._fault_active = set()     # sides currently reporting fault != 0
        self._live = defaultdict(lambda: deque(maxlen=LIVENESS_HISTORY))
        # (topic, field) -> stride-sampled (t, value, driving) history for
        # dead-sensor judgement (see LIVENESS_STRIDE_S)
        # Phase 3 ML scorer (category 9, see _check_ml below). Fail-soft:
        # without built artifacts this is None and every other check is
        # unaffected. Deliberately NOT warmed up: the AE window (4s) and IF
        # rebuild in seconds, and replaying history into them buys nothing
        # over the few seconds of live traffic it takes to fill them.
        self._ml = None
        if MLScorer is not None:
            try:
                self._ml = MLScorer()
            except Exception:
                self._ml = None
        self._last_cmd = {}            # motor -> last commanded_velocity seen
        self._maneuver_until = 0.0     # wall-clock t; current/voltage checks excused until this
        self._last_seen = {}           # topic -> last t a message arrived on it
        self._topic_intervals = defaultdict(lambda: deque(maxlen=50))  # topic -> recent inter-arrival gaps
        self._topic_msg_count = defaultdict(int)      # topic -> messages seen (for coverage check)
        self._field_valid_count = defaultdict(int)    # (topic, field) -> times seen as a valid number

    def _is_driving(self):
        """True if any motor's last-known commanded_velocity is nonzero.
        Used to split the statistical baseline into idle/driving pools
        (see _check_statistical_fields) - gated the same way the handoff's
        item 5 proposed: |commanded_velocity| nonzero, not a hand-picked
        speed threshold, so it needs no per-field tuning."""
        return any(isinstance(v, (int, float)) and v != 0 for v in self._last_cmd.values())

    def _suppressed_while_faulted(self, topic):
        """True if statistical/residual learning should pause for this topic.

        While a motor controller reports a hardware fault, its telemetry is
        the fault, not normal operation (e.g. supply_voltage collapsed for
        the whole 878s UNDERVOLTAGE window in the reference bag). Feeding
        those samples into the split rolling baselines teaches them that
        the fault IS normal, and leaves them contaminated for a full
        WINDOW_SECONDS after the fault clears - so split-pool feeding and
        judgement both pause while the fault bit is set. Same pattern as
        maneuver suppression (item 5). The unified pool keeps feeding
        throughout (it is never z-scored, only is_flat()-checked), so
        dead-sensor detection stays live through a fault; the two
        driving-gated liveness fields necessarily pause with their drive
        pool. Fail-open: a side suppresses only once its status topic has
        actually reported fault != 0 (ingest() runs _check_hardware_fault
        before these checks). The battery is power-system-wide, so it
        suppresses while ANY side is faulted."""
        if not self._fault_active:
            return False
        if topic == "/battery/state":
            return True
        if topic.startswith("/left/"):
            return "left" in self._fault_active
        if topic.startswith("/right/"):
            return "right" in self._fault_active
        return False

    def warm_up(self, telemetry_by_topic):
        """Pre-fill statistical baselines from historical data (e.g. loaded
        from the DB) before live samples start arriving. `telemetry_by_topic`
        is {topic: [(t, data), ...]}, oldest first, t as unix seconds so
        durations compare sanely against WINDOW_SECONDS. Only seeds the
        direct per-field stats used by _check_statistical_fields - does NOT
        emit any events (historical data should never surface as a fresh
        health event), and does NOT warm up the idle/driving
        state-split pools used for anomaly judgement (see
        _check_statistical_fields) - that would require attributing every
        historical sample to its drive-state at that moment, which needs
        the same matched cmd/feedback time-ordering the residual replay
        below already does per motor; extending it to every monitored
        field stays deferred. Net effect: right after warm_up(), the
        unified pool, the residual baselines, and the non-gated liveness
        stride pools are warm, but the split pools start cold and need
        MIN_BASELINE_SAMPLES fresh live samples in each state before
        statistical judgement resumes.

        Residual baseline IS warmed up here (unlike the split pools above):
        cmd and feedback rows are both in telemetry_by_topic already, so
        replaying matched pairs is just a merge walk, not new data
        plumbing - see below. Seeds self._stats["residual:*"] plus
        _last_cmd (so _is_driving() and the residual check start from the
        robot's actual last-known command, not empty). Maneuver history
        (_maneuver_until) is deliberately NOT reconstructed - fail-open:
        a fresh session judges the first seconds normally rather than
        excusing them on stale maneuver grounds."""
        for topic, rows in telemetry_by_topic.items():
            fields = MONITORED_FIELDS.get(topic)
            if not fields:
                continue
            for t, data in rows:
                for field in fields:
                    val = data.get(field)
                    if isinstance(val, (int, float)) and math.isfinite(val):
                        self._stats[f"stat:{topic}:{field}"].push(t, val)
        self._warm_up_liveness(telemetry_by_topic)
        self._warm_up_residuals(telemetry_by_topic)

    def _warm_up_liveness(self, telemetry_by_topic):
        """Seed stride pools from history so dead-sensor judgement doesn't
        start blind every reconnect (same reason as the stat warm-up).
        Non-gated fields only, tagged not-driving: attributing historical
        samples to drive-state needs the cmd merge the split pools already
        deferred, and a wrongly-driving-tagged seed is worse than a cold
        gated pool (which refills its drive filter from live traffic)."""
        for topic, rows in telemetry_by_topic.items():
            fields = MONITORED_FIELDS.get(topic)
            if not fields:
                continue
            for t, data in rows:
                for field in fields:
                    if field in DRIVING_GATED_LIVENESS_FIELDS:
                        continue
                    if field not in SENSOR_LIVENESS_FIELDS:
                        continue
                    val = data.get(field)
                    if not isinstance(val, (int, float)) or not math.isfinite(val):
                        continue
                    if isinstance(val, bool):
                        continue
                    pool = self._live[(topic, field)]
                    if not pool or t - pool[-1][0] >= LIVENESS_STRIDE_S:
                        pool.append((t, val, False))

    def _warm_up_residuals(self, telemetry_by_topic):
        """Replay matched cmd/feedback pairs from history into the residual
        baselines (see warm_up()). Merge walk per motor: command rows hold
        the last-known command (same last-known semantics as live
        ingestion), feedback rows contribute |measured - commanded| against
        it. Also seeds _last_cmd so _is_driving() starts truthful."""
        motors = {}  # motor -> {"cmd": [(t, v)], "fb": [(t, v)]}
        for topic, rows in telemetry_by_topic.items():
            motor = CMD_MOTOR.get(topic)
            if motor is not None:
                slot = motors.setdefault(motor, {"cmd": [], "fb": []})["cmd"]
                for t, data in rows:
                    cv = data.get("commanded_velocity")
                    if isinstance(cv, (int, float)) and math.isfinite(cv):
                        slot.append((t, cv))
                continue
            motor = FEEDBACK_MOTOR.get(topic)
            if motor is not None:
                slot = motors.setdefault(motor, {"cmd": [], "fb": []})["fb"]
                for t, data in rows:
                    mv = data.get("measured_velocity")
                    if isinstance(mv, (int, float)) and math.isfinite(mv):
                        slot.append((t, mv))
        for motor, streams in motors.items():
            if streams["cmd"]:
                self._last_cmd[motor] = streams["cmd"][-1][1]
            merged = sorted(
                [(t, "cmd", v) for t, v in streams["cmd"]]
                + [(t, "fb", v) for t, v in streams["fb"]]
            )
            last_cv = None
            for t, kind, v in merged:
                if kind == "cmd":
                    last_cv = v
                elif last_cv is not None:
                    self._stats[f"residual:{motor}"].push(t, abs(v - last_cv))

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
        events += self._check_comms_stale(topic, t)
        events += self._check_hardware_fault(topic, data, t)
        events += self._check_statistical_fields(topic, data, t)
        events += self._check_sensor_liveness(topic, data, t)
        events += self._check_velocity_residual(topic, data, t)
        events += self._check_gnss_fix(topic, data, t)
        events += self._check_ml(topic, data, t)
        events += self._check_failure_escalation(t)
        return events

    def _emit_new(self, key, kind, severity, signal, message, t, capability=None):
        ev = {
            "key": key, "kind": kind, "severity": severity, "signal": signal,
            "message": message, "capability": capability, "started_at": t,
            "escalated": False,
        }
        self._active[key] = ev
        return {**ev, "state": "new"}

    def _emit_cleared(self, key):
        """Returns a list (not a single event): clearing an event that had
        been escalated to a `failure` also clears the paired failure event,
        so callers always get every state change this produced."""
        old = self._active.pop(key)
        out = [{**old, "state": "cleared"}]
        if old.get("escalated"):
            fail_key = f"failure:{key}"
            fail_ev = self._active.pop(fail_key, None)
            if fail_ev is not None:
                out.append({**fail_ev, "state": "cleared"})
        return out

    def _check_ml(self, topic, data, t):
        """Category 9: multivariate ML verdict (Isolation Forest OR
        Autoencoder, ml/infer.py MLScorer) fused as one `ml:anomaly` event.

        Only ML_TOPICS are forwarded; everything else never affects a tick.
        Deliberately NOT suppressed while a hardware fault is active (unlike
        the statistical/residual baselines): the frozen model doesn't learn,
        so fault samples can't contaminate anything, and its fault-window
        response (96% sustained, 0.1s delay, validated Phase 2/3) is an
        independent detection path worth keeping alongside the bitmask.
        Persistence reuses the shared 2s debounce; capability=None keeps it
        out of failure escalation (no labeled precedent for an ML escalation
        window - Phase 4 question, not invented here). No-op when artifacts
        are absent (self._ml is None)."""
        if self._ml is None or topic not in ML_TOPICS:
            return []
        verdict = self._ml.update(topic, data, t)
        if verdict is None:
            return []
        key = "ml:anomaly"
        transition = self._debounced_transition(key, verdict["fused"], t)
        if transition == "fire_new":
            who = "+".join(verdict["contributors"]) or "?"
            if verdict["ae_score"] is not None:
                ae = "%.4f (%.2fx)" % (verdict["ae_score"],
                                       verdict["ae_norm"])
            elif not verdict.get("ae_live", False):
                ae = "warming"
            else:
                ae = "–"  # between eval ticks (AE evaluates every 5th tick)
            msg = ("multivariate anomaly (%s; IF %.3f (%.2fx), AE %s, %s)"
                   % (who, verdict["if_score"], verdict["if_norm"], ae,
                      "driving" if verdict["driving"] else "idle"))
            # Attribution, IF-flagged ticks only: largest deviation under
            # the fitted scaler (display aid, never cause/diagnosis).
            # AE-only verdicts abstain by design - a window-shape flag has
            # no single culprit row.
            if "IF" in verdict["contributors"] and verdict.get("top_feature"):
                msg += " largest deviation: %s" % verdict["top_feature"]
            elif verdict["contributors"] == ["AE"]:
                msg += " shape anomaly (no single culprit)"
            return [self._emit_new(key, "ml", "warning", "ML anomaly", msg,
                                   t, capability=None)]
        elif transition == "fire_cleared":
            return self._emit_cleared(key)
        return []

    def _check_failure_escalation(self, t):
        """See docstring item 8. Scans currently-active events (not just
        the one(s) touched this sample) since an event can cross its
        escalation window on a later, unrelated message."""
        out = []
        for key, ev in list(self._active.items()):
            if ev.get("escalated") or ev.get("capability") is None:
                continue
            threshold = ESCALATION_SECONDS.get(ev["kind"])
            if threshold is None:
                continue
            if t - ev["started_at"] >= threshold:
                ev["escalated"] = True
                duration = t - ev["started_at"]
                fail_ev = {
                    "key": f"failure:{key}", "kind": "failure", "severity": "critical",
                    "signal": ev["signal"],
                    "message": f"{ev['capability']} capability impaired - {ev['message']} (sustained {duration:.0f}s)",
                    "capability": ev["capability"], "started_at": ev["started_at"], "escalated": True,
                }
                self._active[fail_ev["key"]] = fail_ev
                out.append({**fail_ev, "state": "new"})
        return out

    def _check_comms_stale(self, topic, t):
        out = []
        if topic in COMMS_EXEMPT_TOPICS:
            return out  # event-driven infrastructure topic: never tracked, never judged
        self_key = f"comms:{topic}"
        if self_key in self._active:
            out.extend(self._emit_cleared(self_key))  # this topic just reported - it's alive

        prev = self._last_seen.get(topic)
        if prev is not None and t > prev:
            self._topic_intervals[topic].append(t - prev)
        self._last_seen[topic] = t

        for other_topic, last_t in self._last_seen.items():
            if other_topic == topic:
                continue
            intervals = self._topic_intervals.get(other_topic)
            if not intervals:
                continue  # no interval history yet for this topic - nothing to compare against
            sorted_intervals = sorted(intervals)
            median_interval = sorted_intervals[len(sorted_intervals) // 2]
            # Percentile threshold: publish-on-change topics (/cmd) go
            # legitimately quiet while the robot holds a steady command -
            # their gap distribution has a long tail, and a median-based
            # multiplier fires on that tail constantly. Comparing against a
            # high percentile of the topic's OWN gaps instead means "quieter
            # than it has ever actually been" rather than "quieter than its
            # typical rate". STALE_SCOPE controls whether this smarter
            # threshold applies to every topic or only the known
            # publish-on-change /cmd ones; the floor (STALE_FLOOR_SECONDS)
            # still applies in both cases so a slow topic can't be called
            # stale on a sub-second hiccup.
            use_percentile = (
                STALE_SCOPE == "all" or other_topic.endswith("/cmd")
            )
            if use_percentile:
                k = min(len(sorted_intervals) - 1,
                        max(0, int(len(sorted_intervals) * STALE_PERCENTILE / 100.0)))
                recent_peak_interval = sorted_intervals[k]
                threshold = max(STALE_FLOOR_SECONDS, recent_peak_interval * STALE_MULTIPLIER)
            else:
                threshold = max(STALE_FLOOR_SECONDS, STALE_MULTIPLIER * median_interval)
            elapsed = t - last_t
            key = f"comms:{other_topic}"
            if elapsed > threshold and key not in self._active:
                # FEEDBACK CROSS-CHECK (for /cmd topics): silence on a
                # publish-on-change command topic is EXPECTED whenever the
                # command isn't changing - and the motor's own feedback
                # stream is the proof the system is healthy. Rather than
                # inventing a new "is measured tracking commanded" tolerance,
                # reuse this file's existing residual check: if that motor's
                # residual is not currently flagged as anomalous, the robot
                # is demonstrably following its (static) command, so the
                # /cmd silence is treated as expected, not a comms failure.
                motor = CMD_MOTOR.get(other_topic)
                if motor is not None and f"residual:{motor}" not in self._active:
                    continue
                label = other_topic.strip("/").replace("/", " ")
                # Message states whichever basis actually produced `threshold`
                # above - median for continuously-publishing topics, the
                # recent-peak-gap percentile for publish-on-change ones -
                # rather than always citing the median even when it wasn't
                # what triggered the alert.
                if use_percentile:
                    basis = (f"expected roughly every {median_interval:.2f}s, "
                             f"but has gone quiet for longer than any of its last "
                             f"{len(sorted_intervals)} gaps (worst recent: "
                             f"{recent_peak_interval:.2f}s)")
                else:
                    basis = f"expected roughly every {median_interval:.2f}s"
                msg = f"no messages on {other_topic} for {elapsed:.1f}s ({basis})"
                out.append(self._emit_new(key, "comms", "critical", label, msg, t))
        return out

    def _debounced_transition(self, key, is_anomalous, t):
        """Shared debounce bookkeeping. Returns 'fire_new', 'fire_cleared',
        or None. Time-based (DEBOUNCE_SECONDS of continuous wall-clock time
        in the new direction), not sample-count - see the DEBOUNCE_SECONDS
        comment. Rate-invariant: a 50Hz signal and a 1Hz signal get the
        same real-time protection and the same worst-case detection delay."""
        if is_anomalous:
            self._anom_since[key] = self._anom_since.get(key) or t
            self._norm_since[key] = None
        else:
            self._norm_since[key] = self._norm_since.get(key) or t
            self._anom_since[key] = None

        was_active = key in self._active
        if not was_active and self._anom_since.get(key) is not None \
                and t - self._anom_since[key] >= DEBOUNCE_SECONDS:
            return "fire_new"
        if was_active and self._norm_since.get(key) is not None \
                and t - self._norm_since[key] >= DEBOUNCE_SECONDS:
            return "fire_cleared"
        return None

    def _check_hardware_fault(self, topic, data, t):
        side = STATUS_SIDE.get(topic)
        if side is None:
            return []
        fault_byte = data.get("fault", 0) or 0
        prev = self._last_fault_byte.get(side, 0)
        self._last_fault_byte[side] = fault_byte
        # Fault-active tracking for baseline suppression (see
        # _suppressed_while_faulted): fail-open -- a side counts as
        # faulted only once its status topic has actually said so.
        if fault_byte:
            self._fault_active.add(side)
        else:
            self._fault_active.discard(side)
        if fault_byte == prev:
            return []

        key = f"fault:{side}"
        if fault_byte:
            names = [name for bit, name in FAULT_BITS.items() if fault_byte & bit]
            msg = f"Controller fault: {', '.join(names) if names else fault_byte}"
            return [self._emit_new(key, "hardware_fault", "critical", f"{side} motor driver", msg, t, capability="mobility")]
        elif key in self._active:
            return self._emit_cleared(key)
        return []

    def _check_statistical_fields(self, topic, data, t):
        """Statistical (z-score) anomaly check.

        Baseline splitting (idle vs. driving) - added to address the
        largest remaining false-positive source after maneuver suppression
        and the quantization floor (handoff item 5, ~166/276 of the
        remaining new statistical events, mostly supply_voltage): a
        maneuver-suppression *window* only covers the few seconds right
        after a commanded step, but load/current/voltage stay elevated
        for as long as the robot keeps driving, not just at the instant
        it started. A baseline that mixes idle samples and driving samples
        together is therefore too tight for driving and too loose for
        idle, whichever way the mix currently leans. Fix: maintain two
        separate RollingStat pools per field - one fed only while
        _is_driving() is False, one only while True - and judge each
        sample against whichever pool matches the current state. Gated on
        "any commanded_velocity nonzero", same as the rest of this file's
        no-hand-picked-thresholds convention; not a separate constant.
        The single unified stat per field (self._stats[key], no state
        suffix) is still maintained and still fed unconditionally,
        because _check_sensor_liveness deliberately needs to see the
        *whole* window regardless of driving state to judge whether a
        field is truly frozen versus just currently in a quiet state.
        The debounce/active-event key stays the unsuffixed name too, so a
        field transitioning between idle and driving mid-anomaly doesn't
        reset its consecutive-sample counters or open a second event."""
        out = []
        maneuvering = t < self._maneuver_until
        sensitive = MANEUVER_SENSITIVE_FIELDS.get(topic, ())
        state_suffix = "drive" if self._is_driving() else "idle"
        self._topic_msg_count[topic] += 1
        for field in MONITORED_FIELDS.get(topic, []):
            val = data.get(field)
            if not isinstance(val, (int, float)) or not math.isfinite(val):
                continue
            self._field_valid_count[(topic, field)] += 1
            if maneuvering and field in sensitive:
                continue  # excused: current/voltage jump is expected during a commanded maneuver
            key = f"stat:{topic}:{field}"

            # Unified pool: sensor-liveness check reads this one, so it
            # must keep seeing every sample regardless of driving state -
            # and regardless of fault suppression below, so dead-sensor
            # detection stays live through a fault. Safe to keep feeding:
            # the unified pool is never z-scored, only is_flat()-checked.
            self._stats[key].push(t, val)

            if self._suppressed_while_faulted(topic):
                continue  # fault-active: split (judgement) baselines skip
                          # this sample - it is the fault, not normal data

            # State-split pool: only this one is used to judge anomaly-ness,
            # so idle and driving each get their own, uncontaminated baseline.
            split_stat = self._stats[f"{key}:{state_suffix}"]
            z = split_stat.zscore(val)
            split_stat.push(t, val)
            if z is None:
                continue
            is_anomalous = abs(z) > Z_THRESHOLD
            transition = self._debounced_transition(key, is_anomalous, t)
            if transition == "fire_new":
                label = topic.strip("/").replace("/", " ")
                msg = f"{field} unusual: {val:.2f} (z={z:.1f} vs recent baseline)"
                out.append(self._emit_new(key, "statistical", "warning", f"{label} — {field}", msg, t,
                                           capability=STATISTICAL_CAPABILITY.get(topic)))
            elif transition == "fire_cleared":
                out.extend(self._emit_cleared(key))
        return out

    def field_coverage_warnings(self, min_topic_messages=100):
        """Schema/coverage check - separate from sensor liveness on
        purpose. Sensor liveness (_check_sensor_liveness) needs a field to
        have arrived at least MIN_BASELINE_SAMPLES times before it can
        even ask "has this gone flat"; a field that NEVER arrives at all
        (wrong/missing name for this message version, e.g. this bag's
        /battery/state predating the `temperature` field - confirmed by
        checking the extracted data directly) never reaches that check in
        the first place, so it was previously invisible everywhere - not
        flagged dead, not flagged anything, just silently absent.

        Returns a list of {topic, field, message} dicts for any (topic,
        field) in MONITORED_FIELDS that has never once arrived as a valid
        number, despite the topic itself having published at least
        min_topic_messages messages (so a field that's merely slow to
        start, or a topic that hasn't said much yet, isn't flagged
        prematurely). `message` includes the live running count so it's
        informative in a one-off log line, but callers that want to
        dedupe repeated calls (e.g. main.py checking this periodically
        during a long live session) should dedupe on (topic, field), not
        on the message text, since the count keeps changing."""
        warnings = []
        for topic, fields in MONITORED_FIELDS.items():
            if self._topic_msg_count.get(topic, 0) < min_topic_messages:
                continue
            for field in fields:
                if (topic, field) in KNOWN_ABSENT_FIELDS:
                    continue
                if self._field_valid_count.get((topic, field), 0) == 0:
                    warnings.append({
                        "topic": topic,
                        "field": field,
                        "message": (
                            f"{topic}: '{field}' has never arrived as a valid number "
                            f"in {self._topic_msg_count[topic]} messages on this topic - "
                            f"check the field name against the message actually being published"
                        ),
                    })
        return warnings

    def _check_sensor_liveness(self, topic, data, t):
        """Dead/suspect sensor detection over stride-sampled per-field pools
        (self._live - see LIVENESS_STRIDE_S - NOT the RollingStat pools, so
        the 90s span requirement holds at any publish rate; the old pool
        reuse broke above ~10Hz when the 900-sample cap covered under 90s).

        DRIVING-GATED LIVENESS for thermal fields - confirmed against real
        data, not assumed: replaying the full bag showed channel_temperature
        changes almost every sample while the robot is active (median gap
        between value-changes ~0.2-0.4s, real 15-20 degree C range), but
        genuinely plateaus for very long stretches (case observed: 37
        minutes straight) once the robot goes idle - simple thermal
        settling, not a sensor problem. 87% of this check's
        channel_temperature fires (41/47) landed in exactly that idle tail.
        A quantization-resolution-based span scaling (the original idea for
        this) can't fix that, because the problem isn't resolution, it's
        that "flat" is the CORRECT state while idle. Fix: fields in
        DRIVING_GATED_LIVENESS_FIELDS are judged for flatness only over
        samples recorded while driving - i.e. "has this stopped moving
        while the robot is under load", not "has this stopped moving at
        all". Every other field is judged over the whole pool.

        PROVEN-DEAD vs SUSPECT (see PROVEN_DEAD_SENSORS): a flat reading
        whose value matches a registered physically-impossible sentinel
        fires kind "sensor_failure" (critical, capability "supervision",
        escalates immediately - the 90s span IS the persistence proof).
        Any other flat reading fires "sensor_health" (warning, no
        capability, never escalates): suspicion, not proof."""
        out = []
        driving = self._is_driving()
        for field in MONITORED_FIELDS.get(topic, []):
            val = data.get(field)
            if not isinstance(val, (int, float)) or not math.isfinite(val):
                continue
            if isinstance(val, bool):
                continue
            if field not in SENSOR_LIVENESS_FIELDS and field not in DRIVING_GATED_LIVENESS_FIELDS:
                continue
            # Stride-sampled pool (see LIVENESS_STRIDE_S): one sample per
            # stride second with the driving state at that moment, so the
            # 90s span requirement holds at any publish rate - PLUS every
            # sample whose value differs from the last pooled one. Without
            # that second clause the stride was NOT verdict-neutral: a value
            # that ticked 56->57->56 between two stride instants was never
            # seen, so a live sensor read as flat. Measured on the reference
            # bag: 3 false channel_temperature sensor_health fires (LR 2410s,
            # LF 2446s, LF 3212s) whose full-rate driving samples moved
            # between 56/57 and 58-60 degC while the 1/s samples did not.
            # A frozen field never differs from its last pooled value, so
            # this adds nothing for a really dead sensor (span/rate
            # invariance unchanged); a changing field is not flat anyway.
            pool = self._live[(topic, field)]
            if not pool or t - pool[-1][0] >= LIVENESS_STRIDE_S or val != pool[-1][1]:
                pool.append((t, val, driving))
                while len(pool) > LIVENESS_HISTORY:
                    pool.popleft()
                cutoff = t - LIVENESS_WINDOW_S
                while pool and pool[0][0] < cutoff:
                    pool.popleft()
            key = f"sensor:{topic}:{field}"
            if field in DRIVING_GATED_LIVENESS_FIELDS:
                # "stopped moving while under load" - idle samples excluded
                vals = [(tt, vv) for tt, vv, dr in pool if dr]
            else:
                vals = [(tt, vv) for tt, vv, _ in pool]
            dead = (
                len(vals) >= MIN_BASELINE_SAMPLES
                and vals[-1][0] - vals[0][0] >= DEAD_SENSOR_MIN_SPAN_SECONDS
                and all(v == vals[0][1] for _, v in vals)
            )
            was_active = key in self._active
            if dead and not was_active:
                label = topic.strip("/").replace("/", " ")
                spec = PROVEN_DEAD_SENSORS.get((topic, field))
                if spec is not None and abs(val - spec[0]) <= spec[1]:
                    msg = (f"{field} hasn't changed in over {DEAD_SENSOR_MIN_SPAN_SECONDS:.0f}s - "
                           f"confirmed dead sensor ({spec[2]}), not a robot state")
                    out.append(self._emit_new(key, "sensor_failure", "critical",
                                              f"{label} — {field}", msg, t,
                                              capability="supervision"))
                else:
                    msg = (f"{field} hasn't changed in over {DEAD_SENSOR_MIN_SPAN_SECONDS:.0f}s - "
                           f"sensor may be dead/disconnected, not a robot state")
                    out.append(self._emit_new(key, "sensor_health", "warning",
                                              f"{label} — {field}", msg, t))
            elif not dead and was_active:
                out.extend(self._emit_cleared(key))
        return out

    def _check_velocity_residual(self, topic, data, t):
        if topic in CMD_MOTOR:
            motor = CMD_MOTOR[topic]
            cv = data.get("commanded_velocity")
            if isinstance(cv, (int, float)):
                prev = self._last_cmd.get(motor)
                if isinstance(prev, (int, float)) and abs(cv - prev) > MANEUVER_CMD_DELTA_THRESHOLD:
                    self._maneuver_until = max(self._maneuver_until, t + MANEUVER_SETTLE_SECONDS)
                self._last_cmd[motor] = cv
            return []

        motor = FEEDBACK_MOTOR.get(topic)
        if motor is None:
            return []
        if self._suppressed_while_faulted(topic):
            return []  # fault-active: gap samples would contaminate the residual baseline
        mv = data.get("measured_velocity")
        cv = self._last_cmd.get(motor)
        if not isinstance(mv, (int, float)) or not isinstance(cv, (int, float)):
            return []
        if t < self._maneuver_until:
            return []  # excused: tracking lag right after a commanded step is expected, not a fault

        residual = abs(mv - cv)
        key = f"residual:{motor}"
        stat = self._stats[key]
        z = stat.zscore(residual)
        stat.push(t, residual)
        if z is None:
            return []

        is_anomalous = z > Z_THRESHOLD  # one-sided: only unusually LARGE gaps matter
        transition = self._debounced_transition(key, is_anomalous, t)
        if transition == "fire_new":
            msg = f"commanded vs measured velocity gap unusual: {residual:.2f} rad/s (z={z:.1f})"
            return [self._emit_new(key, "residual", "warning", f"{motor} motor", msg, t, capability="mobility")]
        elif transition == "fire_cleared":
            return self._emit_cleared(key)
        return []

    def _check_gnss_fix(self, topic, data, t):
        if topic != "/ublox/gnss_status":
            return []
        fix_type = data.get("fixType")
        if isinstance(fix_type, bool) or not isinstance(fix_type, int):
            return []
        key = "gnss:fix"
        degraded = fix_type in GNSS_DEGRADED_FIX
        transition = self._debounced_transition(key, degraded, t)
        if transition == "fire_new":
            name = FIX_TYPES.get(fix_type, f"fixType={fix_type}")
            msg = f"gps position fix lost ({name})"
            return [self._emit_new(key, "gnss", "warning", "GPS / GNSS", msg, t, capability="localization")]
        elif transition == "fire_cleared":
            return self._emit_cleared(key)
        return []
