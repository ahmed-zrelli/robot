"""Single source of truth for the ML anomaly-detection feature vector.

Everything that touches the ML pipeline - training (ml/train.py), inference
(ml/infer.py), and the health_monitor.py fusion hook - imports FEATURES from
here instead of keeping its own copy. This is the direct fix for the bug
found in Project B: its anomaly_detector_node.py used a 28-name FEATURE_INFO
dict that didn't match the names its own classify_anomaly() branched on
('velocity_diff', 'current_diff', etc. never existed), so most explanation
text silently fell through to the wrong category. That can't happen here
because there is only one list, and anything that loads a trained model
must prove its feature list matches this one before it's allowed to run
(see require_matching_schema below).

FIELD SELECTION - grounded in the real reference bag
(2026-06-10-10-54-06.bag, 3h32, read directly, not assumed from message
definitions or from Project B's list). 23 fields: more than health_monitor.
py's MONITORED_FIELDS (15, deliberately narrow, tuned for z-score checks),
fewer than Project B's 28 (28 included dead sensors, a near-duplicate pair,
and the fault bitmask as a model input).

Excluded, with the reason confirmed against the real bag data - NOT guessed:

- motor_temperature (all 4 motors): constant -4.176 degC, std=0.0, for the
  entire 3h32 recording, on every one of left/right x front/rear. Sensor is
  not wired on this hardware. Zero information; would also risk a
  divide-by-zero in MinMax scaling on a zero-range column.
- commanded_velocity: 0.968-correlated with measured_velocity in this bag.
  Keeping both roughly doubles one signal instead of adding a second one.
  measured_velocity is kept; the commanded/measured gap is already computed
  elsewhere as the residual check (health_monitor.py item 4) and stays a
  diagnostic signal, not a raw ML input.
- motor_power: only 0.41-correlated with motor_current (not a duplicate),
  but lower diagnostic value for the size of the vector; cut for leanness,
  easy to re-add later if evaluation shows it's missed.
- battery temperature: doesn't exist. health_monitor.py's docstring
  describes it as a dead/frozen sensor, but this bag's /battery/state
  message (an older sensor_msgs/BatteryState variant - has `location` and
  `serial_number` fields instead) has no `temperature` field at all. Same
  practical outcome (exclude it), different root cause - worth checking
  main.py's _sanitize() for wherever a missing field is defaulting to 0
  and presenting as "frozen".
- driver status `status`, `internal_voltage`, `adc_voltage`: `status` is
  effectively constant (128-129 the whole run). `internal_voltage` and
  `adc_voltage` are constant 0.0 - not populated in this message version.
- driver status `fault` (the roboteq fault bitmask): real signal, but used
  as GROUND TRUTH for evaluation only (health_monitor.py category 1 already
  decodes it directly), never as a model input feature. Feeding it to the
  model would let it "cheat" by reading the fault flag instead of learning
  a genuine multivariate precursor - this was Project B's actual bug.
- GNSS lat/lon/height/hMSL: position, not health. Also lat/lon returned NaN
  in places in this bag (likely during no-fix periods) - a separate thing
  worth investigating later, not a reason to include them anyway.
- GNSS velN/velE/velD/gSpeed/horAcc/verAcc/velAcc: all constant 0.0 in this
  recording - not populated at all, so ground-speed cross-checks or GPS
  accuracy-based quality features aren't usable with this data source.
- GNSS diffMode/diffSource/carrierStatus: all constant 0.0 - RTK correction
  detail fields not populated in this bag.

DATA-QUALITY NOTE - supply_voltage needs cleaning before use, not exclusion.
In this bag, ~6.35% of supply_voltage samples on every feedback topic read
1.9-2.0V while the true operating range is 49-54V (confirmed against
/battery/state.voltage, which stays 52.7-53.2V rock-steady throughout) -
a sensor/comms dropout glitch, not real power loss. This field is already
in health_monitor.py's MONITORED_FIELDS, so this glitch may already be
contributing false statistical events there too - worth checking
independently of the ML work. See clean_supply_voltage() below.
"""

FEATURE_SCHEMA_VERSION = "1.0"

# Matches health_monitor.py's own per-motor naming (FEEDBACK_MOTOR / CMD_MOTOR
# dicts) so the two systems stay easy to cross-reference.
_MOTORS = [
    ("LF", "/left/front/feedback"),
    ("LR", "/left/rear/feedback"),
    ("RF", "/right/front/feedback"),
    ("RR", "/right/rear/feedback"),
]

# A supply_voltage reading below this is a dropout artifact, not a real
# under-voltage event - real operating range in the reference bag is
# 49-54V; the confirmed dropout cluster sits at 1.9-2.0V. Real under-voltage
# faults are already caught directly via the roboteq fault bitmask
# (health_monitor.py category 1, FAULT_BITS['UNDERVOLTAGE']), not by this
# field's raw value, so clamping dropouts here doesn't hide a real fault
# path - it just stops a comms glitch from posing as a training sample.
SUPPLY_VOLTAGE_DROPOUT_FLOOR = 40.0

# Ordered (name, topic, field, unit). Order is the ML feature vector's
# index order - do not reorder without bumping FEATURE_SCHEMA_VERSION,
# because that changes what index N means to every already-trained model.
FEATURES = [
    ("bat_voltage", "/battery/state", "voltage", "V"),
    ("bat_current", "/battery/state", "current", "A"),
    ("bat_percentage", "/battery/state", "percentage", "%"),
]
for _motor, _topic in _MOTORS:
    FEATURES += [
        (f"motor_{_motor}_current", _topic, "motor_current", "A"),
        (f"motor_{_motor}_supply_voltage", _topic, "supply_voltage", "V"),
        (f"motor_{_motor}_supply_current", _topic, "supply_current", "A"),
        (f"motor_{_motor}_channel_temp", _topic, "channel_temperature", "C"),
    ]
FEATURES += [
    ("driver_L_ic_temp", "/left_roboteq_driver/status", "ic_temperature", "C"),
    ("driver_R_ic_temp", "/right_roboteq_driver/status", "ic_temperature", "C"),
    ("gnss_fix_type", "/ublox/gnss_status", "fixType", ""),
    ("gnss_num_sv", "/ublox/gnss_status", "numSV", ""),
]

FEATURE_NAMES = [f[0] for f in FEATURES]
FEATURE_COUNT = len(FEATURES)
FEATURE_INDEX = {name: i for i, (name, *_rest) in enumerate(FEATURES)}

# Topics the ML pipeline needs a live/last-known value from, in order to
# build one feature vector. Same topics health_monitor.py already
# subscribes to (see PANEL_MAP in main.py) - no new ROS subscriptions
# needed for Phase 1/2.
REQUIRED_TOPICS = sorted({f[1] for f in FEATURES})

# Ground-truth-only fields. Never put these in FEATURES / feed them to a
# model. Kept here, explicitly, so "should this be a feature?" has one
# documented answer instead of being re-litigated per script.
GROUND_TRUTH_ONLY_FIELDS = {
    ("/left_roboteq_driver/status", "fault"),
    ("/right_roboteq_driver/status", "fault"),
}


def clean_supply_voltage(value):
    """Treat a supply_voltage reading below SUPPLY_VOLTAGE_DROPOUT_FLOOR as
    a dropout artifact rather than a real value. Returns None for dropout
    samples - callers should carry forward the last valid reading (the
    same treatment a stale/frozen sensor would get), not feed None into a
    model or scaler.
    """
    if value is None:
        return None
    if value < SUPPLY_VOLTAGE_DROPOUT_FLOOR:
        return None
    return value


def require_matching_schema(model_meta):
    """Hard stop, not a warning. Call this before running ANY inference
    with a loaded model. model_meta is the metadata.json saved alongside
    the model artifacts at training time (see ml/train.py, Phase 2) -
    expected to contain at least {"schema_version": ..., "features": [...]}.

    Raises FeatureSchemaMismatch on any disagreement. Do not catch this
    and fall back to running anyway - a silently mismatched feature vector
    is exactly what produced Project B's dead classify_anomaly() branches
    (it was tuned against one feature list while the deployed vector was
    built from a different one).
    """
    model_version = model_meta.get("schema_version")
    model_features = model_meta.get("features")

    if model_version != FEATURE_SCHEMA_VERSION:
        raise FeatureSchemaMismatch(
            f"model was trained against feature schema version "
            f"{model_version!r}, runtime is {FEATURE_SCHEMA_VERSION!r}"
        )
    if model_features != FEATURE_NAMES:
        missing = [f for f in FEATURE_NAMES if f not in (model_features or [])]
        extra = [f for f in (model_features or []) if f not in FEATURE_NAMES]
        raise FeatureSchemaMismatch(
            "model feature list does not match features.py FEATURES "
            f"(missing here vs model: {missing}, extra in model: {extra})"
        )
    return True


class FeatureSchemaMismatch(Exception):
    """Raised by require_matching_schema(). Callers must not run inference
    when this is raised - see the docstring above."""
