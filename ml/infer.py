"""Phase 3 inference: online IF + Autoencoder scoring for PGuard telemetry.

Standalone scorer (no HealthMonitor dependency): feed it one message at a
time via update(), exactly as messages arrive. It owns the cross-topic
last-known cache, the tick gate, vdrop assembly, scaling, both models, and
per-state thresholding, and returns one verdict per tick. Fusion into
HealthMonitor as category 9 (debounce + event emission) is the caller's
job - see health_monitor.py.

Locked decisions (pre-Phase-3 review):
- onnxruntime for the AE (parity with torch verified to 3.6e-06 during
  Phase 2); torch stays training-only and is never imported here.
- Single fused verdict per tick (IF OR AE, matching metadata.json's fusion
  definition); contributors listed for the event signal text.
- No failure escalation for the ML verdict (capability=None at the
  HealthMonitor layer): no labeled precedent for an ML escalation window;
  revisit in Phase 4.
- Live value set: 14 raw fields over 5 topics (battery current/voltage +
  4x motor_current/supply_current/raw supply_voltage). There is NO live
  23-field vector: require_matching_schema() is a load-time metadata
  comparison, so GNSS/percentage/thermals/status-derived values never need
  live cache entries. Staleness gating (max_age_s) applies to the 14 raw
  fields only.
- vdrop uses RAW supply_voltage, NEVER clean_supply_voltage(): nulling
  would flatline a real collapse into invisibility (see Phase 2 findings).
  Enforced by construction (the cleaning function is not imported) and by
  test (vdrop tracks a raw sub-floor reading 1:1).
- AE cold start: IF scores from the first tick; AE joins once 20 scaled
  vectors are buffered (4s at 5Hz). Fused = IF OR (AE if ready).
- AE evaluates every 5th tick (stride 5), matching the stride train.py used
  to build calibration windows, with the verdict attributed to the eval
  tick only (mirrors train.py's window-flag-to-last-row assignment). The
  first carried-verdict version of this amplified each firing ~5x and was
  reverted same-session. Between evals AE abstains; IF scores every tick.

Usage:
    scorer = MLScorer()  # loads ml/artifacts/, validates schema
    verdict = scorer.update(topic, data, t)  # None most calls; dict on ticks
"""

import json
import pickle
from collections import deque
from pathlib import Path

import numpy as np

ARTIFACTS_DIR = Path(__file__).resolve().parent / "artifacts"

# Raw live fields the ML path consumes: (cache key, topic, message field).
# Mirrors train.py's FEATURE_SRC for exactly these keys (pinned by
# test_infer.TestRawFieldMapping) - every other schema field is either
# validated once at load (require_matching_schema) or never used here.
RAW_FIELDS = [
    ("bat_voltage", "/battery/state", "voltage"),
    ("bat_current", "/battery/state", "current"),
    ("LF_motor_current", "/left/front/feedback", "motor_current"),
    ("LF_supply_current", "/left/front/feedback", "supply_current"),
    ("LF_supply_voltage_raw", "/left/front/feedback", "supply_voltage"),
    ("LR_motor_current", "/left/rear/feedback", "motor_current"),
    ("LR_supply_current", "/left/rear/feedback", "supply_current"),
    ("LR_supply_voltage_raw", "/left/rear/feedback", "supply_voltage"),
    ("RF_motor_current", "/right/front/feedback", "motor_current"),
    ("RF_supply_current", "/right/front/feedback", "supply_current"),
    ("RF_supply_voltage_raw", "/right/front/feedback", "supply_voltage"),
    ("RR_motor_current", "/right/rear/feedback", "motor_current"),
    ("RR_supply_current", "/right/rear/feedback", "supply_current"),
    ("RR_supply_voltage_raw", "/right/rear/feedback", "supply_voltage"),
]
assert len(RAW_FIELDS) == 14

# 13-vector assembly order (must match train.py ML_FEATURES, pinned by
# test_schema_contract against metadata.json's ml_features):
# [bat_current, motor currents x4, supply currents x4, vdrop x4]
_MOTORS = ("LF", "LR", "RF", "RR")
ML_FEATURES = (
    ["bat_current"]
    + [f"motor_{m}_current" for m in _MOTORS]
    + [f"motor_{m}_supply_current" for m in _MOTORS]
    + [f"motor_{m}_vdrop" for m in _MOTORS]
)
assert len(ML_FEATURES) == 13

# /cmd topics for the driving predicate (same rule as
# HealthMonitor._is_driving: any last-known commanded_velocity != 0).
CMD_TOPICS = {
    "/left/front/cmd", "/left/rear/cmd",
    "/right/front/cmd", "/right/rear/cmd",
}

# Topics HealthMonitor must forward to MLScorer.update(). Everything else
# never affects a tick (update() would only run the time gate on
# irrelevant traffic, shifting tick phase for no reason).
ML_TOPICS = frozenset([t for _, t, _ in RAW_FIELDS] + list(CMD_TOPICS))


class MLScorer:
    """Online anomaly scorer. Not thread-safe; call update() from one loop."""

    def __init__(self, artifacts_dir=ARTIFACTS_DIR):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                               / "backend"))
        from features import require_matching_schema  # noqa: E402

        artifacts_dir = Path(artifacts_dir)
        with open(artifacts_dir / "metadata.json") as f:
            self.meta = json.load(f)
        # Load-time schema gate: raises FeatureSchemaMismatch on any
        # version/feature-list disagreement. Runs once here, never per tick.
        require_matching_schema(self.meta)

        with open(artifacts_dir / "scaler.pkl", "rb") as f:
            self.scaler = pickle.load(f)
        with open(artifacts_dir / "if_model.pkl", "rb") as f:
            self.if_model = pickle.load(f)

        import onnxruntime as ort
        self._ort = ort.InferenceSession(
            str(artifacts_dir / "ae_model.onnx"),
            providers=["CPUExecutionProvider"],
        )
        self._ort_in = self._ort.get_inputs()[0].name

        self.tick_dt = 1.0 / float(self.meta["grid_hz"])
        self.max_age_s = float(self.meta["max_age_s"])
        self.ae_window = int(self.meta["ae_window"])
        self.thr_if = self.meta["thresholds"]["if"]
        self.thr_ae = self.meta["thresholds"]["ae"]

        self._cache = {}       # cache key -> (t, value)
        self._last_cmd = {}    # cmd topic -> last commanded_velocity
        self._last_tick = None
        self._window = deque(maxlen=self.ae_window)
        self._tick_count = 0
        self._ae_evaluated = False  # ever produced an AE verdict (vs cold)

    # -- ingestion ------------------------------------------------------
    def _is_driving(self):
        return any(isinstance(v, (int, float)) and v != 0
                   for v in self._last_cmd.values())

    def update(self, topic, data, t):
        """Feed one message. Returns None (no tick) or a verdict dict:
        {"t", "driving", "if_score", "if_flag", "ae_score" (None until the
        window fills), "ae_flag", "fused", "contributors"}. All scores are
        raw (higher = more anomalous); flags already apply the per-state
        thresholds. Persistence/debounce is the caller's job."""
        if topic in CMD_TOPICS:
            cv = data.get("commanded_velocity")
            if isinstance(cv, (int, float)) and not isinstance(cv, bool):
                self._last_cmd[topic] = cv
            return None

        for key, src_topic, field in RAW_FIELDS:
            if src_topic == topic:
                val = data.get(field)
                if isinstance(val, bool):
                    continue
                if isinstance(val, (int, float)) and np.isfinite(val):
                    self._cache[key] = (t, float(val))

        if self._last_tick is not None and t - self._last_tick < self.tick_dt:
            return None
        return self._try_tick(t)

    # -- scoring --------------------------------------------------------
    def _try_tick(self, t):
        if len(self._cache) < len(RAW_FIELDS):
            return None
        for key in self._cache:
            ts, _ = self._cache[key]
            if t - ts > self.max_age_s:
                return None  # stale input: skip the tick, don't score junk
        vec = self._assemble()
        if vec is None:
            return None
        self._last_tick = t
        self._tick_count += 1

        z = self.scaler.transform(vec.reshape(1, -1))[0]
        if_score = float(-self.if_model.score_samples(z.reshape(1, -1))[0])
        self._window.append(z)

        driving = self._is_driving()
        state = "drive" if driving else "idle"
        if_flag = bool(if_score > self.thr_if[state])

        # AE evaluates every 5th tick (AE_STRIDE_MATCH = 5, same stride the
        # calibration windows were built with). This mirrors the offline
        # attribution exactly: a window verdict belongs to its last row
        # only (cf. train.py's f_ae_rows[ae_row_idx] assignment). Between
        # evals there is deliberately NO carried verdict - carrying one
        # would amplify each firing ~5x (measured: AE-only 2.06% stays flat
        # instead of dropping). Before the first eval there is none either.
        ae_ready = len(self._window) >= self.ae_window
        ae_score, ae_flag = None, False
        if ae_ready and self._tick_count % 5 == 0:
            w = np.concatenate(list(self._window)).reshape(1, -1).astype(
                np.float32)
            rec = self._ort.run(None, {self._ort_in: w})[0]
            ae_score = float(((rec - w) ** 2).mean())
            ae_flag = bool(ae_score > self.thr_ae[state])
            self._ae_evaluated = True

        contributors = []
        if if_flag:
            contributors.append("IF")
        if ae_flag:
            contributors.append("AE")
        # Threshold-relative multiples (display only - flags above already
        # applied these thresholds; borrowed from the sibling project's
        # iso_norm/ae_norm readouts, which are genuinely good UX).
        if_norm = if_score / self.thr_if[state]
        ae_norm = (ae_score / self.thr_ae[state]
                   if ae_score is not None else None)
        # Top-deviation attribution, IF-flagged ticks only: largest
        # |robust-z| under the FITTED scaler (center_/scale_ already
        # loaded - zero new parameters, zero new thresholds). Worded as
        # deviation, never cause/diagnosis. AE-only ticks abstain: a
        # 20-sample shape flag has no single culprit row by construction.
        top_feature = self._top_feature(vec) if if_flag else None
        return {
            "t": t,
            "driving": driving,
            "if_score": if_score,
            "if_flag": if_flag,
            "if_norm": if_norm,
            "ae_score": ae_score,
            "ae_ready": ae_ready,
            "ae_live": self._ae_evaluated,
            "ae_flag": ae_flag,
            "ae_norm": ae_norm,
            "top_feature": top_feature,
            "fused": bool(if_flag or ae_flag),
            "contributors": contributors,
        }

    def _top_feature(self, vec):
        """Name of the largest-deviation ML feature under the fitted
        RobustScaler. Pure display aid for IF-flagged ticks."""
        center = np.asarray(self.scaler.center_, dtype=float)
        scale = np.asarray(self.scaler.scale_, dtype=float)
        scale = np.where(scale > 1e-12, scale, 1e-12)
        z = np.abs((vec - center) / scale)
        return ML_FEATURES[int(np.argmax(z))]

    def _assemble(self):
        """13-vector in train.py ML_FEATURES order. vdrop uses RAW supply
        values by construction - clean_supply_voltage() is never imported
        into this module (see module docstring)."""
        try:
            c = {k: self._cache[k][1] for k, _, _ in RAW_FIELDS}
        except KeyError:
            return None
        bat_v = c["bat_voltage"]
        out = [c["bat_current"]]
        out += [c[f"{m}_motor_current"] for m in _MOTORS]
        out += [c[f"{m}_supply_current"] for m in _MOTORS]
        out += [c[f"{m}_supply_voltage_raw"] - bat_v for m in _MOTORS]
        vec = np.array(out, dtype=float)
        if not np.all(np.isfinite(vec)):
            return None
        return vec
