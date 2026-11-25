"""Phase 2 trainer: Isolation Forest + PyTorch Autoencoder for PGuard telemetry.

Implements the revised Phase 2 decisions (Handover v7 + expert review,
plus corrections measured during training -- see header notes on the
13-field vdrop vector and the dropout-artifact correction):
  5a. Common-grid as-of resample (5 Hz, battery tolerance 0.6s / 0.3s
      others) with max-age staleness rule; forward-fill of tolerance-miss
      NaNs; 13-field stationary ML vector (9 currents + 4 harness-drop
      differentials; discrete GNSS, monotonic SoC, drifting thermals and
      absolute voltages excluded with documented rationale each).
      Full 23-field schema v1.0 is still validated; the model consumes a
      documented projection of it (see ML_FEATURES / metadata.json).
  5b. Fault window 3125.6-4004.4s (UNDERVOLTAGE, fault byte 4, verified:
      zero nonzero fault bytes elsewhere in the bag) excluded with +-60s
      embargo. Eval framed as single-event detection-delay + sustained
      fraction, NOT general recall.
  5c. Chronological 70/15/15 blocks over the normal-only timeline with 60s
      purge gaps between splits. No shuffling. IF on single rows; AE on
      sliding windows (20 samples @ 5 Hz = 4 s, stride 5 = 1 s).
  5d. RobustScaler (fit on train only) + near-zero-range guard. Thresholds
      by target-FPR quantile on val (0.5% per model -> ~1% OR-fusion,
      measured jointly, not assumed). Implied k (= (thr-mu)/sigma) reported
      for health_monitor convention compatibility. k=3 is NOT fixed.
  5e. Gate: per-state point-FPR + events/hour (2 s persistence, mirrors
      DEBOUNCE_SECONDS) + detection delay + sustained fractions. Fault
      window eval reports three numbers: delay from window start, sustained
      fraction excluding the 3 driving sub-segments, driving-sub-segment
      flag fraction separately (A-variant, locked).
  5f. Artifacts + metadata.json (schema version, feature lists, scaler,
      thresholds, k, mu/sigma, window config, embargo, seeds, bag id,
      val FPR curve). require_matching_schema() gates inference.
  5g. One balanced model (driving oversampled to >=30% of train), two
      per-state point thresholds (idle/drive via the same _is_driving
      predicate as health_monitor.py), min-sample fallback to pooled.
      No segment-max statistics. Burst noise handled by inference-side
      persistence, not calibration warping.

Usage:  python backend/../ml/train.py   (run from PROJECT_ROOT)
Inputs: backend/data/*.json + manifest.json (extract_bag.py output)
Outputs: ml/artifacts/{scaler.pkl, if_model.pkl, ae_model.pt,
         ae_model.onnx, metadata.json, threshold_curve.csv, train_log.txt}

Determinism: SEED fixed; torch single-threaded + deterministic flags.
"""

import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import RobustScaler

# ---------------------------------------------------------------- paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "backend" / "data"
OUT_DIR = Path(__file__).resolve().parent / "artifacts"
OUT_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(PROJECT_ROOT / "backend"))
from features import (  # noqa: E402
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    SUPPLY_VOLTAGE_DROPOUT_FLOOR,
)

# ---------------------------------------------------------------- constants (all logged to metadata.json)
SEED = 42
GRID_HZ = 5.0
GRID_DT = 1.0 / GRID_HZ
ASOF_TOL_S = 0.3        # per-topic sample must be within 300 ms of a grid tick
ASOF_TOL_OVERRIDE = {"/battery/state": 0.6}  # 1 Hz topic: half period + margin
MAX_AGE_S = 2.0         # ... and no older than 2 s (staleness rule)
FAULT_START_S = 3125.6  # verified UNDERVOLTAGE window (fault byte 4)
FAULT_END_S = 4004.4
EMBARGO_S = 60.0        # guard band around the fault window
SPLIT_FRACS = (0.70, 0.15, 0.15)
SPLIT_GAP_S = 60.0      # purge gap between train|val and val|test
AE_WINDOW = 20          # 20 samples @ 5 Hz = 4 s
AE_STRIDE = 5           # 1 s
TARGET_FPR_PER_MODEL = 0.005   # 0.5% each -> ~1% OR-fusion (measured jointly)
MIN_STATE_SAMPLES = 25  # fallback gate: reuses RollingStat min_samples convention
PERSIST_S = 2.0         # event persistence for events/hour + detection (cf. DEBOUNCE_SECONDS)
DRIVE_MIN_FRAC = 0.30   # oversample driving rows in IF train pool up to this
IF_TREES = 200
AE_HIDDEN = 128
AE_LATENT = 32
AE_LR = 1e-3
AE_MAX_EPOCHS = 100
AE_PATIENCE = 10
AE_BATCH = 256

# GNSS fields are discrete/environment signals, not continuous health
# signals: excluded from the ML vector, stay rule-based (health_monitor
# category 3). The 23-field schema v1.0 is untouched -- this is a documented
# projection of it, recorded in metadata.json as "ml_features".
# ML vector v2 (13 features, stationary-by-construction). Verified
# diagnosis for the revision: all 6 absolute voltage channels sag
# coherently by ~0.35V over the 3h32 run (battery discharge physics;
# 53.13->52.76V at the battery, 52.62->52.29V at the motors) while idle
# per-bin sd is ~0.18V. A late-idle sample sits ~2 sigma from the
# early-calibrated envelope on 6 correlated dims at once -> 26% FPR on
# late data. No fixed offline model on absolute levels survives that.
# Fix, physics-grounded: HARNESS-DROP differential features
#   vdrop_M = supply_voltage_M(raw, uncleaned) - bat_voltage
# Common-mode discharge drift cancels (early -0.51V vs late -0.47V: 0.04V
# drift vs ~0.2V sd = 0.2 sigma, stationary), while the fault signature is
# preserved and amplified (fault: ~8V - 53V = -45V, ~200 sigma). Absolute
# voltages are dropped from the vector entirely.
# Glitch handling WITHOUT imputation and WITHOUT deletion: rows where any
# raw supply_voltage < SUPPLY_VOLTAGE_DROPOUT_FLOOR are KEPT with their raw
# vdrop values. Lesson learned the hard way (measured): dropping them also
# deletes the fault itself -- a sustained undervoltage collapse is
# per-sample identical to the glitch, and the drop rule left 1 surviving
# fault row. Separation mechanism instead:
#   - threshold calibration uses CLEAN rows only (glitch mask excluded),
#     so the 99.5th percentile sits in real normal data, not glitches;
#   - at inference, single-sample glitches score high but die in the 2s
#     persistence filter, while sustained collapse survives and alerts.
# Train/infer transforms stay identical (plain subtraction). The glitch-row
# flag fraction is reported separately from the gated clean-row FPR.
# CORRECTION to Handover v7 §4 (measured this session, LF topic: 3886 rows
# <40V, of which 3880 inside the fault window and 6 outside): the "~6.35%
# dropout artifact" is overwhelmingly THE FAULT ITSELF, not a comms glitch.
# Battery steadiness (52.7-53.2V throughout) does NOT distinguish the two
# -- the battery is equally steady during the real undervoltage event; the
# sag is downstream (harness/controllers/load), which is exactly what
# vdrop measures, and the controller's own UNDERVOLTAGE bitmask confirms
# it is real. True isolated glitch rate outside the window: ~0.01%
# (6 rows / 3.5h on LF, scattered 2-38V). The mask therefore stays as
# transient protection for future bags, not as a major data cleaner.
# PHASE 3 CONSTRAINT (from the above): ml/infer.py must NOT apply
# features.clean_supply_voltage() to vdrop inputs. Null-and-carry-forward
# would turn a real 878s collapse into a flat line at the last good value
# -- hiding the primary fault signature from the model. (Live code is
# unaffected: clean_supply_voltage is currently defined but never called.)
# KEPT (13): bat_current (idle-stationary at -3.9A; driving spread absorbed
# by per-state thresholds), 4 x motor_current (fault x19 variance
# inflation), 4 x supply_current, 4 x vdrop.
# Also excluded, same slow-drift rationale as before: bat_percentage
# (monotonic 74.7->61.6% ramp -- dashboard gauge, not a model input) and
# the 6 thermal fields (~9C cooling; tracked by health_monitor's adaptive
# baselines, categories 2/6). ML owns fast multivariate signatures, rules
# own slow levels. Revisit with multi-run training data.
EXCLUDED_FROM_ML = (
    ("gnss_fix_type", "gnss_num_sv")  # discrete/environment -> rules
    + ("bat_percentage",)             # monotonic ramp -> dashboard gauge
    + tuple(f"motor_{m}_channel_temp" for m in ("LF", "LR", "RF", "RR"))
    + ("driver_L_ic_temp", "driver_R_ic_temp")  # slow drift -> rules
    + ("bat_voltage",)                # discharge drift -> see vdrop
    + tuple(f"motor_{m}_supply_voltage" for m in ("LF", "LR", "RF", "RR"))
                                      # absolute -> replaced by vdrop
)
ML_CURRENT_FEATURES = (
    ["bat_current"]
    + [f"motor_{m}_current" for m in ("LF", "LR", "RF", "RR")]
    + [f"motor_{m}_supply_current" for m in ("LF", "LR", "RF", "RR")]
)
ML_VDROP_MOTORS = ("LF", "LR", "RF", "RR")
ML_FEATURES = ML_CURRENT_FEATURES + [f"motor_{m}_vdrop" for m in ML_VDROP_MOTORS]
assert len(ML_FEATURES) == 13, ML_FEATURES
ML_TRANSFORM_NOTE = ("vdrop_M = supply_voltage_M(raw, uncleaned) - "
                     "bat_voltage; glitch rows (any raw supply_voltage < "
                     "FLOOR) kept with raw values; thresholds calibrated "
                     "on clean rows only; persistence separates glitch "
                     "transients from sustained collapse. PHASE3: do NOT "
                     "apply clean_supply_voltage() to vdrop inputs.")

# feature -> (topic, field); mirrors FEATURES in features.py
FEATURE_SRC = {
    "bat_voltage": ("/battery/state", "voltage"),
    "bat_current": ("/battery/state", "current"),
    "bat_percentage": ("/battery/state", "percentage"),
    "driver_L_ic_temp": ("/left_roboteq_driver/status", "ic_temperature"),
    "driver_R_ic_temp": ("/right_roboteq_driver/status", "ic_temperature"),
}
for _m in ("LF", "LR", "RF", "RR"):
    _t = {  # noqa: keep mapping explicit per motor
        "LF": "/left/front/feedback", "LR": "/left/rear/feedback",
        "RF": "/right/front/feedback", "RR": "/right/rear/feedback",
    }[_m]
    FEATURE_SRC[f"motor_{_m}_current"] = (_t, "motor_current")
    FEATURE_SRC[f"motor_{_m}_supply_voltage"] = (_t, "supply_voltage")
    FEATURE_SRC[f"motor_{_m}_supply_current"] = (_t, "supply_current")
    FEATURE_SRC[f"motor_{_m}_channel_temp"] = (_t, "channel_temperature")

# Continuously-publishing topics constrain the grid span and the staleness
# rule. /cmd topics are publish-on-change (silent for minutes while the
# robot holds a command -- cf. _check_comms_stale in health_monitor.py):
# they must NOT constrain the span and their silence is NOT staleness.
# Their value is latched last-known, exactly like HealthMonitor._last_cmd.
CONTINUOUS_TOPICS = [
    "/battery/state",
    "/left/front/feedback", "/left/rear/feedback",
    "/right/front/feedback", "/right/rear/feedback",
    "/left_roboteq_driver/status", "/right_roboteq_driver/status",
]
CMD_TOPICS = ["/left/front/cmd", "/left/rear/cmd",
              "/right/front/cmd", "/right/rear/cmd"]

TOPIC_FILE = {
    "/battery/state": "battery_state.json",
    "/left/front/feedback": "left_front_feedback.json",
    "/left/rear/feedback": "left_rear_feedback.json",
    "/right/front/feedback": "right_front_feedback.json",
    "/right/rear/feedback": "right_rear_feedback.json",
    "/left_roboteq_driver/status": "left_roboteq_driver_status.json",
    "/right_roboteq_driver/status": "right_roboteq_driver_status.json",
    "/left/front/cmd": "left_front_cmd.json",
    "/left/rear/cmd": "left_rear_cmd.json",
    "/right/front/cmd": "right_front_cmd.json",
    "/right/rear/cmd": "right_rear_cmd.json",
}

rng = np.random.default_rng(SEED)
torch.manual_seed(SEED)
torch.use_deterministic_algorithms(True)
torch.set_num_threads(1)

LOG = []


def log(msg):
    print(msg, flush=True)
    LOG.append(msg)


# ---------------------------------------------------------------- 1. load + resample
def load_topic(name):
    rows = json.loads((DATA_DIR / TOPIC_FILE[name]).read_text())
    df = pd.DataFrame(rows).sort_values("_t").reset_index(drop=True)
    return df


def build_grid(df_dict):
    """5 Hz common grid over the continuous topics' overlap.

    Per-topic as-of merge with tolerance (values NaN outside ASOF_TOL_S,
    later forward-filled); a grid row is stale only if a CONTINUOUS topic
    has no sample within MAX_AGE_S. /cmd topics are publish-on-change:
    excluded from span and staleness, latched last-known downstream.
    """
    cont = {t: df_dict[t] for t in CONTINUOUS_TOPICS}
    t0 = max(df["_t"].iloc[0] for df in cont.values())
    t1 = min(df["_t"].iloc[-1] for df in cont.values())
    grid = np.arange(t0, t1, GRID_DT)
    base = pd.DataFrame({"_t": grid})
    stale_any = np.zeros(len(grid), dtype=bool)
    cols = {}
    for topic, df in cont.items():
        tol = ASOF_TOL_OVERRIDE.get(topic, ASOF_TOL_S)
        m = pd.merge_asof(
            base, df, on="_t", direction="nearest",
            tolerance=tol,
        )
        # merge_asof keeps only the left key; re-derive the true match age
        # from the raw timestamps for the staleness rule (MAX_AGE_S).
        raw = df["_t"].values
        idx = np.clip(np.searchsorted(raw, grid), 0, len(raw) - 1)
        prev = np.clip(idx - 1, 0, len(raw) - 1)
        best = np.where(
            np.abs(raw[idx] - grid) <= np.abs(raw[prev] - grid), idx, prev
        )
        nearest_age = np.abs(raw[best] - grid)
        stale_any |= nearest_age > MAX_AGE_S
        cols[topic] = m
    return base, cols, stale_any, t0, t1


def extract_feature_matrix():
    df_dict = {t: load_topic(t) for t in TOPIC_FILE}
    base, cols, stale_any, t0, t1 = build_grid(df_dict)
    grid = base["_t"].values
    n = len(grid)
    X = np.full((n, len(ML_FEATURES)), np.nan)
    col = {}
    for j, feat in enumerate(ML_CURRENT_FEATURES):
        topic, field = FEATURE_SRC[feat]
        vals = cols[topic][field].values.astype(float)  # NaN outside tolerance
        col[feat] = vals
        X[:, j] = vals
    # harness-drop differentials from RAW supply_voltage (uncleaned) minus
    # battery voltage; glitch rows are KEPT (flagged in the glitch mask for
    # calibration/eval stratification) -- see header comment.
    batV = cols["/battery/state"]["voltage"].values.astype(float)
    glitch = np.zeros(n, dtype=bool)
    for k, _m in enumerate(ML_VDROP_MOTORS):
        _t, _ = FEATURE_SRC[f"motor_{_m}_supply_voltage"]
        raw_sup = cols[_t]["supply_voltage"].values.astype(float)
        glitch |= (raw_sup < SUPPLY_VOLTAGE_DROPOUT_FLOOR)
        X[:, len(ML_CURRENT_FEATURES) + k] = raw_sup - batV
    log(f"sensor-glitch rows flagged (any raw supply_voltage < "
        f"{SUPPLY_VOLTAGE_DROPOUT_FLOOR:.0f}V): {int(glitch.sum())} "
        f"({glitch.mean() * 100:.2f}%) -- kept, stratified at "
        f"calibration/eval; in-fault-window: "
        f"{int((glitch & ((grid >= FAULT_START_S) & (grid <= FAULT_END_S))).sum())}")
    # driving flag: same predicate as HealthMonitor._is_driving
    # (any motor's last-known commanded_velocity != 0)
    cmd_vals = {}
    for ct in ("/left/front/cmd", "/left/rear/cmd",
               "/right/front/cmd", "/right/rear/cmd"):
        df = df_dict[ct]
        raw_t, raw_v = df["_t"].values, df["commanded_velocity"].values
        idx = np.searchsorted(raw_t, grid, side="right") - 1
        idx = np.clip(idx, 0, len(raw_t) - 1)
        cmd_vals[ct] = np.where(idx >= 0, raw_v[idx], 0.0)
    driving = np.zeros(n, dtype=bool)
    for v in cmd_vals.values():
        driving |= (v != 0)
    # fault flag from both driver status topics (ground truth only)
    fault = np.zeros(n, dtype=bool)
    for st in ("/left_roboteq_driver/status", "/right_roboteq_driver/status"):
        df = df_dict[st]
        raw_t = df["_t"].values
        raw_f = df["fault"].fillna(0).values.astype(float)
        idx = np.searchsorted(raw_t, grid, side="right") - 1
        idx = np.clip(idx, 0, len(raw_t) - 1)
        fault |= (raw_f[idx] != 0)
    return grid, X, driving, fault, stale_any, glitch


# ---------------------------------------------------------------- helpers
def persistence_filter(flags, win):
    """Keep only runs of True lasting >= win consecutive grid steps."""
    if win <= 1:
        return flags
    c = np.convolve(flags.astype(int), np.ones(win, dtype=int), mode="same")
    # convolve 'same' smears edges; do exact run-length filtering instead:
    out = np.zeros_like(flags)
    i, n = 0, len(flags)
    while i < n:
        if flags[i]:
            j = i
            while j < n and flags[j]:
                j += 1
            if j - i >= win:
                out[i:j] = True
            i = j
        else:
            i += 1
    return out


def quantile_thr(scores, fpr):
    return float(np.quantile(scores, 1.0 - fpr))


def per_state_thresholds(scores, driving_mask, fpr):
    out = {}
    pooled = quantile_thr(scores, fpr)
    out["pooled"] = pooled
    for state, m in (("idle", ~driving_mask), ("drive", driving_mask)):
        if m.sum() >= MIN_STATE_SAMPLES:
            out[state] = quantile_thr(scores[m], fpr)
        else:
            out[state] = pooled
            out[state + "_fallback"] = True
    mu, sd = float(scores.mean()), float(scores.std() + 1e-12)
    out["mu"], out["sigma"] = mu, sd
    out["k_idle"] = (out["idle"] - mu) / sd
    out["k_drive"] = (out["drive"] - mu) / sd
    out["k_pooled"] = (pooled - mu) / sd
    return out


class AE(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.enc = nn.Sequential(nn.Linear(dim, AE_HIDDEN), nn.ReLU(),
                                 nn.Linear(AE_HIDDEN, AE_LATENT), nn.ReLU())
        self.dec = nn.Sequential(nn.Linear(AE_LATENT, AE_HIDDEN), nn.ReLU(),
                                 nn.Linear(AE_HIDDEN, dim))

    def forward(self, x):
        return self.dec(self.enc(x))


def window_scores(model, Zw, device):
    model.eval()
    with torch.no_grad():
        rec = model(torch.from_numpy(Zw.astype(np.float32)).to(device))
        err = ((rec.cpu().numpy() - Zw) ** 2).mean(axis=1)
    return err


# ---------------------------------------------------------------- main
def main():
    log("== Phase 2 training (revised spec) ==")
    manifest = json.loads((DATA_DIR / "manifest.json").read_text())
    log(f"bag duration: {manifest['duration_s']:.1f}s")

    grid, X, driving, fault, stale_any, glitch = extract_feature_matrix()
    log(f"grid rows @ {GRID_HZ:.0f}Hz: {len(grid)}  features: {len(ML_FEATURES)}")
    log(f"stale-dropped rows (>{MAX_AGE_S}s from a topic sample): "
        f"{stale_any.sum()} ({stale_any.mean() * 100:.2f}%)")
    nan_before = np.isnan(X).sum()
    # forward-fill tolerance-miss NaNs; drop leading-NaN rows. Glitch rows
    # are KEPT (stratified at calibration/eval, never deleted).
    Xf = pd.DataFrame(X).ffill().values
    valid = ~(stale_any | np.isnan(Xf).any(axis=1))
    log(f"dropout/NaN cells pre-fill: {nan_before} "
        f"({nan_before / X.size * 100:.2f}%); rows kept: {valid.sum()}/{len(grid)}")
    grid, Xf, driving, fault, glitch = (grid[valid], Xf[valid],
                                        driving[valid], fault[valid],
                                        glitch[valid])

    # ground-truth audit: fault bytes outside the known window?
    fmask = (grid >= FAULT_START_S) & (grid <= FAULT_END_S)
    n_out = int((fault & ~fmask).sum())
    log(f"fault-flagged grid rows: {int(fault.sum())}  inside window: "
        f"{int((fault & fmask).sum())}  outside window: {n_out}")
    if n_out:
        g_out = grid[fault & ~fmask]
        d_edge = min(float(np.abs(g_out - FAULT_START_S).min()),
                     float(np.abs(g_out - FAULT_END_S).min()))
        log(f"  outside rows lie {d_edge:.2f}s from the window edge "
            f"(as-of latch: the clear is observed on the next status sample)")
        assert d_edge <= 5.0, "fault flags far outside the known window"

    # normal-only pool + embargo around the fault window
    emb = (grid >= FAULT_START_S - EMBARGO_S) & (grid <= FAULT_END_S + EMBARGO_S)
    normal = ~emb
    log(f"embargo-excluded rows (+-{EMBARGO_S:.0f}s): {emb.sum()}; normal pool: {normal.sum()}")
    idx = np.where(normal)[0]
    # State-stratified chronological split: the driving pool (t < ~3122s)
    # and the idle pool each split 70/15/15 in time order, then merged.
    # Pure tail-slicing would leave val/test with 0% driving (measured),
    # making drive-threshold calibration impossible. Within-state order is
    # chronological with purge gaps, so no near-duplicate leakage; across
    # states there is interleaving by construction (different states are
    # not near-duplicates of each other).
    drv_idx = idx[driving[idx]]
    idle_idx = idx[~driving[idx]]

    def _blocked_split(pool):
        n = len(pool)
        n_tr = int(n * SPLIT_FRACS[0])
        n_va = int(n * SPLIT_FRACS[1])
        gap = int(SPLIT_GAP_S * GRID_HZ)
        # gap in rows overshoots in sparse pools; cap at 5% of pool
        gap = min(gap, max(1, n // 20))
        n_te = int(n * SPLIT_FRACS[2])
        s_tr = pool[:max(1, n_tr - gap)]
        s_va = pool[n_tr:n_tr + max(1, n_va - gap)]
        s_te = pool[n_tr + n_va:n_tr + n_va + max(1, n_te)]
        return s_tr, s_va, s_te, gap

    (d_tr, d_va, d_te, g_d) = _blocked_split(drv_idx)
    (i_tr, i_va, i_te, g_i) = _blocked_split(idle_idx)
    tr = np.sort(np.concatenate([d_tr, i_tr]))
    va = np.sort(np.concatenate([d_va, i_va]))
    te = np.sort(np.concatenate([d_te, i_te]))
    log(f"split sizes (rows): train {len(tr)} (drive {len(d_tr)}, idle {len(i_tr)})  "
        f"val {len(va)} (drive {len(d_va)}, idle {len(i_va)})  "
        f"test {len(te)} (drive {len(d_te)}, idle {len(i_te)})")
    log(f"  purge gaps: drive {g_d} rows, idle {g_i} rows (~{SPLIT_GAP_S:.0f}s target)")
    for name, s in (("train", tr), ("val", va), ("test", te)):
        dfrac = driving[s].mean() * 100
        log(f"  {name}: t {grid[s[0]]:.1f}->{grid[s[-1]]:.1f}s  drive {dfrac:.1f}%")

    # scaler: fit on train only
    scaler = RobustScaler()
    Ztr = scaler.fit_transform(Xf[tr])
    Zva = scaler.transform(Xf[va])
    Zte = scaler.transform(Xf[te])
    ranges = (Xf[tr].max(axis=0) - Xf[tr].min(axis=0))
    assert (ranges > 1e-9).all(), f"near-zero-range column in train: {ranges}"
    log("RobustScaler fit on train only; range guard passed.")

    # ---- Isolation Forest (single rows), driving oversampled to >=30%
    dfrac = driving[tr].mean()
    tr_if = tr.copy()
    if dfrac < DRIVE_MIN_FRAC:
        need = int((DRIVE_MIN_FRAC * len(tr) - driving[tr].sum()) / (1 - DRIVE_MIN_FRAC))
        pool = tr[driving[tr]]
        extra = rng.choice(pool, size=need, replace=True)
        tr_if = np.concatenate([tr, extra])
        rng.shuffle(tr_if)
    log(f"IF train pool: {len(tr_if)} rows "
        f"(drive {driving[tr_if].mean() * 100:.1f}%, oversampled from {dfrac * 100:.1f}%)")
    iff = IsolationForest(n_estimators=IF_TREES, max_samples="auto",
                          contamination="auto", random_state=SEED)
    iff.fit(scaler.transform(Xf[tr_if]))
    s_if_va = -iff.score_samples(Zva)
    s_if_te = -iff.score_samples(Zte)

    # ---- Autoencoder on sliding windows of the train block
    def make_windows(Z, drv, gl):
        ws, ds, gs = [], [], []
        for s in range(0, len(Z) - AE_WINDOW + 1, AE_STRIDE):
            w = Z[s:s + AE_WINDOW]
            if np.isnan(w).any():
                continue
            ws.append(w.reshape(-1))
            ds.append(drv[s:s + AE_WINDOW].mean() > 0.5)
            gs.append(gl[s:s + AE_WINDOW].any())
        return np.stack(ws), np.array(ds), np.array(gs)

    Wtr, Dtr, Gtr = make_windows(Ztr, driving[tr], glitch[tr])
    Wva, Dva, Gva = make_windows(Zva, driving[va], glitch[va])
    Wte, Dte, Gte = make_windows(Zte, driving[te], glitch[te])
    log(f"AE windows: train {len(Wtr)} (glitch {int(Gtr.sum())})  "
        f"val {len(Wva)} (glitch {int(Gva.sum())})  "
        f"test {len(Wte)} (glitch {int(Gte.sum())})  "
        f"(len {AE_WINDOW}, stride {AE_STRIDE})")
    device = torch.device("cpu")
    ae = AE(Wtr.shape[1]).to(device)
    opt = torch.optim.Adam(ae.parameters(), lr=AE_LR)
    loss_fn = nn.MSELoss()
    # val windows tensor once -- CLEAN windows only, so one-off -50V
    # glitch spikes don't dominate early-stopping/model selection
    Wva_clean = Wva[~Gva] if (~Gva).sum() else Wva
    Wva_t = torch.from_numpy(Wva_clean.astype(np.float32))
    best, best_state, bad = np.inf, None, 0
    for ep in range(AE_MAX_EPOCHS):
        ae.train()
        perm = rng.permutation(len(Wtr))
        for b in range(0, len(Wtr), AE_BATCH):
            xb = torch.from_numpy(Wtr[perm[b:b + AE_BATCH]].astype(np.float32))
            opt.zero_grad()
            loss = loss_fn(ae(xb), xb)
            loss.backward()
            opt.step()
        ae.eval()
        with torch.no_grad():
            vl = loss_fn(ae(Wva_t), Wva_t).item()
        if vl < best - 1e-6:
            best, best_state, bad = vl, {k: v.cpu().clone() for k, v in ae.state_dict().items()}, 0
        else:
            bad += 1
            if bad >= AE_PATIENCE:
                log(f"AE early stop at epoch {ep + 1}, best val MSE {best:.6f}")
                break
    ae.load_state_dict(best_state)
    s_ae_va = window_scores(ae, Wva, device)
    s_ae_te = window_scores(ae, Wte, device)

    # ---- thresholds: quantile-targeted on CLEAN val rows/windows only,
    # so the 99.5th percentile sits in real normal data, not glitch spikes.
    # IF val states are per-row; AE val states are per-window (Dva).
    clean_va = ~glitch[va]
    clean_wva = ~Gva
    log(f"calibration pools: IF {int(clean_va.sum())}/{len(va)} clean val rows; "
        f"AE {int(clean_wva.sum())}/{len(Wva)} clean val windows")
    thr_if = per_state_thresholds(s_if_va[clean_va], driving[va][clean_va],
                                  TARGET_FPR_PER_MODEL)
    thr_ae = per_state_thresholds(s_ae_va[clean_wva], Dva[clean_wva],
                                  TARGET_FPR_PER_MODEL)
    log(f"IF thresholds: idle {thr_if['idle']:.4f} (k={thr_if['k_idle']:.2f})  "
        f"drive {thr_if['drive']:.4f} (k={thr_if['k_drive']:.2f})  pooled k={thr_if['k_pooled']:.2f}")
    log(f"AE thresholds: idle {thr_ae['idle']:.6f} (k={thr_ae['k_idle']:.2f})  "
        f"drive {thr_ae['drive']:.6f} (k={thr_ae['k_drive']:.2f})  pooled k={thr_ae['k_pooled']:.2f}")
    if "drive_fallback" in thr_if or "drive_fallback" in thr_ae:
        log("WARNING: drive val pool below MIN_STATE_SAMPLES -- pooled fallback used")

    def apply_thr(scores, drv, thr):
        t = np.where(drv, thr["drive"], thr["idle"])
        return scores > t

    # ---- test gate
    f_if = apply_thr(s_if_te, driving[te], thr_if)
    f_ae = apply_thr(s_ae_te, Dte, thr_ae)
    # align AE window flags to rows for fusion: attribute to window's last row
    f_ae_rows = np.zeros(len(te), dtype=bool)
    ae_row_idx = np.arange(AE_WINDOW - 1, len(te), AE_STRIDE)[:len(f_ae)]
    f_ae_rows[ae_row_idx] = f_ae
    fused = f_if | f_ae_rows
    win_p = int(PERSIST_S * GRID_HZ)
    fused_ev = persistence_filter(fused, win_p)

    def state_fpr(flags, drv):
        out = {}
        for state, m in (("idle", ~drv), ("drive", drv)):
            out[state] = float(flags[m].mean()) if m.sum() else float("nan")
        out["pooled"] = float(flags.mean())
        return out

    fpr_if = state_fpr(f_if, driving[te])
    fpr_if_clean = state_fpr(f_if[~glitch[te]], driving[te][~glitch[te]])
    fpr_fused = state_fpr(fused, driving[te])
    fpr_fused_clean = state_fpr(fused[~glitch[te]], driving[te][~glitch[te]])
    # glitch-row flag fraction (informational: transients the persistence
    # layer is expected to kill, not genuine false positives)
    gl_te = glitch[te]
    glitch_flag_frac = float(fused[gl_te].mean()) if gl_te.sum() else float("nan")
    fpr_ev = state_fpr(fused_ev, driving[te])
    test_hours = (grid[te[-1]] - grid[te[0]]) / 3600
    # count events (runs) in persistence-filtered flags
    nev = 0
    i = 0
    while i < len(fused_ev):
        if fused_ev[i]:
            nev += 1
            while i < len(fused_ev) and fused_ev[i]:
                i += 1
        else:
            i += 1
    ev_per_h = nev / test_hours if test_hours else float("nan")
    log(f"TEST point-FPR IF-only: {fpr_if}  (clean rows: {fpr_if_clean})")
    log(f"TEST point-FPR fused: {fpr_fused}  (clean rows: {fpr_fused_clean})")
    log(f"TEST glitch-row flag fraction (transients, pre-persistence): "
        f"{glitch_flag_frac * 100:.1f}% of {int(gl_te.sum())} glitch rows)")
    log(f"TEST event-FPR fused (>{PERSIST_S:.0f}s persistence): {fpr_ev}  "
        f"events: {nev} in {test_hours:.2f}h -> {ev_per_h:.2f}/h")

    # ---- fault-window eval (A-variant, three numbers)
    Zf = scaler.transform(Xf[fmask])
    s_if_f = -iff.score_samples(Zf)
    Wf, _, _ = make_windows(Zf, driving[fmask], glitch[fmask])
    s_ae_f = window_scores(ae, Wf, device) if len(Wf) else np.array([])
    drv_f = driving[fmask]
    g_f = grid[fmask]
    f_if_f = apply_thr(s_if_f, drv_f, thr_if)
    f_ae_f_rows = np.zeros(fmask.sum(), dtype=bool)
    a_idx = np.arange(AE_WINDOW - 1, fmask.sum(), AE_STRIDE)[:len(s_ae_f)]
    if len(s_ae_f):
        thr_w = np.where(driving[fmask][a_idx], thr_ae["drive"], thr_ae["idle"])
        f_ae_f_rows[a_idx] = s_ae_f > thr_w
    fused_f = persistence_filter(f_if_f | f_ae_f_rows, win_p)
    # driving sub-segments inside the window (verified §4): report separately
    in_drive_seg = np.zeros(fmask.sum(), dtype=bool)  # == drv_f; kept explicit
    in_drive_seg = drv_f.copy()
    fault_t0 = 3125.631  # first nonzero fault sample (verified)
    det_idx = np.where(fused_f)[0]
    delay = float(g_f[det_idx[0]] - fault_t0) if len(det_idx) else float("nan")
    sust_all = float(fused_f.mean())
    nomm = ~in_drive_seg
    sust_nomotion = float(fused_f[nomm].mean()) if nomm.sum() else float("nan")
    drive_frac = float(fused_f[in_drive_seg].mean()) if in_drive_seg.sum() else float("nan")
    log(f"FAULT-WINDOW: detection delay {delay:.1f}s from {fault_t0:.3f}s; "
        f"sustained {sust_all * 100:.1f}% overall; {sust_nomotion * 100:.1f}% excl. "
        f"driving segs; driving-seg flag rate {drive_frac * 100:.1f}% "
        f"(n_drive_rows={int(in_drive_seg.sum())})")

    # ---- threshold curve (val sweep, for audit)
    qs = np.linspace(0.90, 0.999, 20)
    curve = []
    for q in qs:
        ti = float(np.quantile(s_if_va, q))
        ta = float(np.quantile(s_ae_va, q))
        curve.append((q, ti, ta, float((s_if_te > ti).mean()),
                      float((s_ae_te > ta).mean())))
    pd.DataFrame(curve, columns=["quantile", "thr_if", "thr_ae",
                                 "test_fpr_if", "test_fpr_ae_window"]
                 ).to_csv(OUT_DIR / "threshold_curve.csv", index=False)

    # ---- save artifacts
    with open(OUT_DIR / "scaler.pkl", "wb") as f:
        pickle.dump(scaler, f)
    with open(OUT_DIR / "if_model.pkl", "wb") as f:
        pickle.dump(iff, f)
    torch.save(ae.state_dict(), OUT_DIR / "ae_model.pt")
    # Remove any previous export first: overwriting ae_model.onnx(.data)
    # in place fails on synced/locked files (measured: Errno 22 on open
    # for write, while fresh creation in the same directory works fine).
    # Deleting first forces the create path. The export itself is retried
    # - transient AV/sync locks can still bite the fresh create.
    for _old in (OUT_DIR / "ae_model.onnx", OUT_DIR / "ae_model.onnx.data"):
        try:
            _old.unlink()
        except FileNotFoundError:
            pass
    dummy = torch.zeros(1, Wtr.shape[1])
    last_err = None
    for _attempt in (1, 2, 3):
        try:
            torch.onnx.export(ae, dummy, OUT_DIR / "ae_model.onnx",
                              input_names=["window"],
                              output_names=["reconstruction"],
                              dynamic_axes={"window": {0: "batch"}})
            last_err = None
            break
        except OSError as e:
            last_err = e
            log(f"ONNX export attempt {_attempt} failed ({e}); retrying")
            import time as _time
            _time.sleep(5)
    if last_err is not None:
        raise last_err
    metadata = {
        "schema_version": FEATURE_SCHEMA_VERSION,
        "features": FEATURE_NAMES,
        "ml_features": ML_FEATURES,
        "ml_feature_index": {f: i for i, f in enumerate(ML_FEATURES)},
        "excluded_from_ml": list(EXCLUDED_FROM_ML),
        "ml_transform": ML_TRANSFORM_NOTE,
        "exclusion_reason": {
            "gnss_fix_type+gnss_num_sv": "discrete/environment; rule-based via health_monitor category 3",
            "bat_percentage": "monotonic discharge ramp 74.7->61.6%; dashboard gauge, not a model input",
            "channel_temperature x4 + ic_temperature x2": "slow ~9C thermal drift; tracked by health_monitor adaptive baselines (cat. 2/6)",
            "bat_voltage + supply_voltage x4 (absolute)": "coherent ~0.35V discharge sag breaks fixed thresholds; replaced by vdrop differentials",
        },
        "grid_hz": GRID_HZ,
        "asof_tol_s": ASOF_TOL_S,
        "max_age_s": MAX_AGE_S,
        "fault_window_s": [FAULT_START_S, FAULT_END_S],
        "embargo_s": EMBARGO_S,
        "split_fracs": list(SPLIT_FRACS),
        "split_gap_s": SPLIT_GAP_S,
        "split_rows": {"train": [int(grid[tr[0]]), int(grid[tr[-1]])],
                       "val": [int(grid[va[0]]), int(grid[va[-1]])],
                       "test": [int(grid[te[0]]), int(grid[te[-1]])]},
        "ae_window": AE_WINDOW,
        "ae_stride": AE_STRIDE,
        "ae_arch": {"input": int(Wtr.shape[1]), "hidden": AE_HIDDEN, "latent": AE_LATENT},
        "if_params": {"n_estimators": IF_TREES, "max_samples": "auto",
                      "contamination": "auto", "random_state": SEED},
        "target_fpr_per_model": TARGET_FPR_PER_MODEL,
        "thresholds": {
            "if": {k: (float(v) if isinstance(v, (int, float, np.floating)) else v)
                   for k, v in thr_if.items()},
            "ae": {k: (float(v) if isinstance(v, (int, float, np.floating)) else v)
                   for k, v in thr_ae.items()},
            "fusion": "OR with per-state thresholds + "
                      f"{PERSIST_S:.0f}s persistence at inference",
        },
        "test_gate": {
            "point_fpr_if": fpr_if,
            "point_fpr_if_clean": fpr_if_clean,
            "point_fpr_fused": fpr_fused,
            "point_fpr_fused_clean": fpr_fused_clean,
            "glitch_row_flag_frac": glitch_flag_frac,
            "event_fpr_fused": fpr_ev,
            "events_per_hour": ev_per_h,
            "fault_detection_delay_s": delay,
            "fault_sustained_frac": sust_all,
            "fault_sustained_frac_excl_driving": sust_nomotion,
            "fault_driving_seg_flag_frac": drive_frac,
        },
        "scaler": "RobustScaler (fit train only)",
        "phase3_constraints": [
            "Do NOT apply features.clean_supply_voltage() to vdrop inputs "
            "(would null the fault signature into a flat line).",
            "Replicate ML_TRANSFORM_NOTE exactly: raw supply_voltage minus "
            "battery voltage; 5Hz grid; RobustScaler from scaler.pkl; "
            "per-state thresholds + 2s persistence before alerting.",
            "Call features.require_matching_schema() on metadata.json "
            "schema_version+features before loading models.",
        ],
        "seed": SEED,
        "bag": {"duration_s": manifest["duration_s"],
                "topics": manifest.get("topic_msgtype", {})},
        "artifacts": ["scaler.pkl", "if_model.pkl", "ae_model.pt",
                      "ae_model.onnx", "threshold_curve.csv"],
    }
    with open(OUT_DIR / "metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)
    with open(OUT_DIR / "train_log.txt", "w") as f:
        f.write("\n".join(LOG) + "\n")
    log(f"artifacts written to {OUT_DIR}")

    # ---- gate verdict (advisory; Phase 3 needs Ahmed's sign-off)
    # clean-row point-FPR + event rate are the gates; raw pooled FPR is
    # reported but not gated (it folds in expected glitch transients).
    ok = (fpr_fused_clean["pooled"] <= 0.02) and (ev_per_h <= 2.0) \
        and (delay == delay) and (delay <= 120.0)
    log(f"GATE {'PASS' if ok else 'REVIEW'}: clean pooled fused point-FPR "
        f"{fpr_fused_clean['pooled'] * 100:.2f}% (budget 2%), "
        f"{ev_per_h:.2f} events/h (budget 2/h), delay {delay:.1f}s "
        f"(budget 120s)")


if __name__ == "__main__":
    main()
