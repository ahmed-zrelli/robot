# PGuard Anomaly Detection — Handover v8

Internship: Enova Robotics (PGuard autonomous security robot). Ahmed, final-year
Cybersecurity Engineering student, TEK-UP University. Supersedes v1–v7 — this is
a **self-contained consolidation**, not a diff. Whoever/whatever picks this up
should need only this file plus the working copy
(`robot_dashboard_v0_21 - Copy/robot_dashboard_v0_20/`, which contains
`backend/data/*.json` extracted from the bag, `ml/artifacts/`, and the test
suite) to continue. The 435MB raw bag (`bag/2026-06-10-10-54-06.bag`) is also
present in the working copy but should not be needed.

**Read §0 before doing anything else.**

---

## 0. Working method — non-negotiable

- **Never state a fact about code/data without checking it directly.** This
  project has hit this failure mode three times:
  - v4: an AI memory summary claimed Phase 2 ML work was already done. It
    wasn't.
  - v7 era: an AI's memory claimed the locked architecture was "sklearn-only
    MLPRegressor, no PyTorch/ONNX" and that `ml/data.py` already existed.
    Neither appeared in any of the 8 real handover documents (all specify
    Isolation Forest + PyTorch Autoencoder + ONNX). Disregarded.
  - This session: an AI's *edit tooling* twice damaged files it was supposed
    to leave intact (merged two `__init__` lines in `health_monitor.py`;
    deleted a function signature in `main.py`). Both caught by running the
    tests immediately after editing. **Run the test suite after every edit.**
- No hardcoded/invented constants — thresholds derived from observed data.
- One phase at a time, validated before moving on.
- Distinguish genuine bugs from deliberate trade-offs before flagging.
- When a document's own numbers don't reproduce from the underlying data,
  trust the data. This handover corrects two v7-era claims from direct
  measurement (§4).
- Golden tests pin behavior: `backend/test_health_monitor.py::TestGoldenReplay`
  (run with `PGUARD_GOLDEN=1`) fails loudly on any detection change. Update
  pinned numbers only deliberately, never to make the test pass.

---

## 1. Project context

- **Robot**: PGuard, Enova Robotics — 250kg 4WD autonomous surveillance robot,
  **ROS1** (confirmed this session — the "reportedly ROS2" line in older docs
  was stale and has been corrected), LiDAR 3D, GPS RTK, IMU, 6 cameras,
  Roboteq motor controllers (4 channels, one per wheel).
- **Project A (Ahmed's, this project)**: telemetry monitoring + anomaly
  detection dashboard. FastAPI + WebSocket backend, vanilla HTML/CSS/JS
  frontend (deliberate choice), Docker-based virtual robot for dev/test,
  ROS1/rosbridge for live telemetry, SQLite persistence, Isolation Forest +
  PyTorch Autoencoder anomaly detection (Phase 2, done — §5).
- **Project B (Anaghim Knani's, sibling/reference project)**: separate ROS1
  pipeline + React/TS dashboard, same robot, same reference bag. Source of
  ideas to evaluate and selectively merge — not to copy. See §8.
- **Backend default mode is `live`** (`main.py`: `TELEMETRY_SOURCE` unset =
  live). Deliberate product decision, not an accident: bag mode with absent
  `backend/data/` is an import-time crash, while live mode with no rosbridge
  degrades to a graceful connect error. Safe-boot beats demo-convenience.
- **Real-world unknowns**: exact ROS1 distro on hardware, motor controller
  package, LiDAR topic/type, whether `rosbridge_server` runs on the real
  robot, what "secure connection" concretely requires. Confirmed: wired
  Ethernet, ROS1, nothing changed on the real robot since the bag.

---

## 2. Phased plan status

| Phase | Content | Status |
|---|---|---|
| **1** | `features.py` — 23-field schema v1.0, `require_matching_schema()` guard, `clean_supply_voltage()` | **Closed.** Locked (§3) |
| **1.5** | Dashboard gaps (dead-field flagging, `/cmd` topics, motor panel fields, `/coil_outputs`/`/nmea_sentence`) | **Closed** |
| **1.6** | `verify_schema_assumptions.py` — semantic-drift guard | Still unwritten / not in build |
| **2** | `ml/train.py` — IF + PyTorch AE, ONNX export, validation gate | **DONE (§5). GATE PASS.** Artifacts in `ml/artifacts/` |
| **2.5** | Review hardening — 15 code fixes + GNSS gate rework + test suite | **DONE (§6). 19/19 tests pass (1 golden)** |
| **3** | `ml/infer.py` — fusion into `health_monitor.py` as category 9 | Not started. Constraints locked (§9) |
| **4** | Evaluation (beyond the Phase 2 gate) | Not started |
| **5** | Frontend — anomaly panel, alert feed, report export | Not started. Note: Ahmed explicitly reversed the unpopulated-row hiding — all rows render always (§6) |

---

## 3. Feature schema

### 3a. Locked 23-field schema v1.0 (`backend/features.py`) — UNCHANGED

- Battery (3): `bat_voltage`, `bat_current`, `bat_percentage`
- Per motor ×4 (16): `motor_{M}_current`, `motor_{M}_supply_voltage`,
  `motor_{M}_supply_current`, `motor_{M}_channel_temp`
- Driver (2): `driver_L_ic_temp`, `driver_R_ic_temp`
- GNSS (2): `gnss_fix_type`, `gnss_num_sv`

Exclusions verified against the real bag: `motor_temperature` (dead sensor,
constant −4.1754°C, std=0.0); `commanded_velocity` (0.968–0.973 correlated
with measured); `motor_power` (leanness); battery temperature (field doesn't
exist in this message variant); driver `status`/`internal_voltage`/
`adc_voltage` (constant); driver `fault` bitmask (**ground truth only** —
fixes Project B's leakage bug); GNSS position/velocity/accuracy fields
(position or constant-0.0 unpopulated). No field/order changes without
bumping `FEATURE_SCHEMA_VERSION`.

### 3b. ML 13-field projection (documented subset, schema untouched)

`ml/train.py` trains on 13 stationary-by-construction signals: `bat_current`,
4× `motor_current`, 4× `supply_current`, plus 4×
`vdrop_M = supply_voltage_M(raw) − bat_voltage` (harness-drop differentials).
Rationale per group, all measured:
- GNSS pair: discrete/environment → rule-based (health category 3).
- `bat_percentage`: monotonic 74.7→61.6% ramp → dashboard gauge, not input.
- 6 thermal fields: ~9°C drift → `health_monitor` adaptive baselines.
- 6 absolute voltages: coherent ~0.35V discharge sag vs 0.18V idle sd
  (measured 26% FPR on late data with absolutes) → replaced by vdrop
  (drift 0.04V vs 0.2V sd; fault reads −45V, ~200σ).
Recorded in `metadata.json` as `ml_features` + `ml_transform`. Inference
builds the full 23-vector, validates schema, projects + subtracts.

---

## 4. Bag verification record — what's actually confirmed

Reference bag `2026-06-10-10-54-06.bag`, 435MB, 12,757.618s (3h32m37.6s).
Identity confirmed (all 17 topics' counts match v3 exactly). Driving: 98
segments, median 0.7s, 84.7% under 10s, 29.3 min total; 3 segments (~27.5s)
inside the fault window at t=3374–3402s. UNDERVOLTAGE window 3125.6–4004.4s
(fault byte 4); zero nonzero fault bytes elsewhere except §4b.

### 4a. CORRECTION — the "6.35% dropout artifact" is the fault itself (v7 §4 was wrong)

v7 claimed ~6.35% of `supply_voltage` samples were a comms-dropout glitch
(1.9–2.0V vs 49–54V operating range), "confirmed" by battery steadiness.
Direct measurement this session (LF topic: 3,886 rows <40V): **3,880 inside
the fault window, 6 outside.** Battery steadiness cannot distinguish glitch
from real downstream undervoltage (battery is equally steady during the real
event); the controller's own UNDERVOLTAGE bitmask confirms it is real. True
isolated glitch rate: ~0.01%. Consequence: `clean_supply_voltage()` nulling
is correct for single-sample blips and **must never touch vdrop inputs at
inference** (it would flatline an 878s collapse into invisibility). Live
code is unaffected (`clean_supply_voltage` is defined but never called).

### 4b. Mid-fault status blip at t≈3357.5s (new ground-truth detail)

Both controllers report fault=0 briefly mid-window (raw status JSON shows
mixed 0/4 samples at t=3355–3360 on both sides). `hardware_fault`
correctly edge-fires clear+new around it (hence 10 hw events, not 2).
Do not "fix" this flapping — it is in the ground-truth stream.

### 4c. GNSS distributions (49,725 status / 49,768 fix rows)

fixType: 2D 71.2% (35,392), GNSS+DR 24.4% (12,143), DR-only 3.6%, TIME_ONLY
0.5% (259), NO_FIX 0.3%; 3D never occurs on this hardware. Fix rows: lat/lon
real (138 NaN rows pre-fix), ellipsoid height real (26–172m); hMSL, gSpeed,
velN/E/D, accuracies, motion/vehicle headings, magDec all bit-exact 0.000
(unpopulated on this receiver path). Battery detail fields: `present` never
true, capacity/charge/design 0, `cell_voltage` always empty, status/health/
tech always 0 — all 12,758 rows. `temperature` field absent (older variant).

---

## 5. Phase 2 — as built (`ml/train.py`, ~700 lines)

Pipeline: 5Hz common-grid as-of resample (battery tolerance 0.6s, else 0.3s;
2s max-age staleness; `/cmd` latched last-known, excluded from span/
staleness — first version of this code applied staleness to publish-on-
change topics and dropped 46% of rows; fixed). Fault window ±60s embargo.
State-stratified chronological 70/15/15 splits with 60s purge gaps (pure
tail-slicing left val/test with 0% driving — measured). RobustScaler (train
only). IF (200 trees) on single rows with driving oversampled to 30%; AE
(280→128→32→128→280) on 20-sample/stride-5 windows with early stopping.
Thresholds by 0.5%-FPR quantile on **clean** rows per state (glitch rows kept
raw, calibrated around, killed at inference by 2s persistence), OR fusion,
per-state idle/drive thresholds with pooled fallback. Fault-window eval uses
the locked A-variant: delay from window start + sustained fraction excluding
driving sub-segments + driving-sub-segment rate reported separately.

**Gate (PASS, all budgets)**: clean fused point-FPR 1.24% (≤2%; idle 1.23%,
drive 1.26%); **0 events in 2.77h** (≤2/h); detection delay **0.13s**
(≤120s); sustained 96.0% excl. driving, 0.0% on driving sub-segments (clean
motion separation). Implied k values logged (do NOT fix k=3 — measured
k_idle≈1.0, k_drive≈7.1 for IF). Artifacts: `scaler.pkl`, `if_model.pkl`,
`ae_model.pt`, `ae_model.onnx` (+`.data`, ONNX checker-clean, matches torch
to 3.6e-06), `metadata.json` (schema, features, transform, thresholds, k,
μ/σ, window/embargo/split config, seeds, phase-3 constraints),
`threshold_curve.csv`, `train_log.txt`. `requirements.txt` gained the ML
deps (torch via CPU wheel index). Known limitation: single fault type,
single event — "96% sustained" is not general recall; AE is effectively an
idle specialist (drive k≈13.4, IF carries driving); drive val pool 970 rows.

---

## 6. Review hardening (v2.5) — what changed and why

1. **Fault-window baseline suppression** (`health_monitor.py` item 9):
   split-pool feeding + judgement pause while a side's fault bit is set
   (fail-open, per-side; battery pauses on any side); unified liveness pool
   keeps feeding (never z-scored). Measured: statistical 244→33; second-
   order effect sensor_health 11→5 (killed clear/refire flapping of dead
   flags around the §4b blip and the real clear — fire-once-and-hold is
   correct for a dead sensor).
2. **Bag-mode DB writes** (`main.py`, `db.py`): one row/topic/2s via
   `_db_write_due()` (time-based so warm-up is speed-invariant; a skip was
   considered and rejected — it would regress `warm_up()` which reads pooled
   history, and `list_sessions()`/`latest_session_id()` which deliberately
   cover telemetry-only sessions). Live mode still records everything.
   Commits batched (200 rows/0.5s) + `atexit` flush.
3. **XSS hardening** (`app.js`): `escapeHtml()` in both health renderers
   (signals embed robot-supplied topic names); `try/catch` on `JSON.parse`.
4. **`KNOWN_ABSENT_FIELDS`** — battery `temperature` no longer warns
   every session; novel absences stay loud.
5. **GNSS gate rework**: `GNSS_DEGRADED_FIX = {0,1,5}` with 2s debounce
   instead of ordinal `< 3` — old form flagged 2D (71% of normal ops) AND
   missed TIME_ONLY (5 > 3, worse than 2D) simultaneously. Measured: 44
   degraded episodes, 29 flicker-filtered, 12 sustained; failure 24→23.
   Fix names in messages via `FIX_TYPES`; bool guard on the field.
6. **Residual warm-up from DB** (`_warm_up_residuals()` merge-walk + `_last_cmd`
   seeding; `_warm_up_health` fetches `/cmd` topics). Maneuver history stays
   fail-open; split pools stay cold (documented).
7. **Relay**: definition-hash cache (comment promised it, code only compared
   type names); skips `/rosout*`/`/rosapi*`; `/diagnostics` kept.
8. **Mid-session re-discovery**: `poll_new_topics()` every 30s, relay-aware
   with raw→relay upgrade, via existing `meta_update`; `close()` resets state.
9. **Speedtest 10s cooldown**; README default-mode + ROS1 corrections; LiDAR
   JSON-relay warning; `peek.py` `_t` fix.
10. **Tests** (`backend/test_health_monitor.py` 12 incl. golden,
    `backend/test_persistence.py` 5; stdlib only): RollingStat, debounce,
    fault decode/clear, per-side suppression (split frozen + unified fed),
    GNSS (sustained fires, blip suppressed, 2/3/4 healthy, fixType-5
    regression), residual replay, KNOWN_ABSENT, golden replay pinning
    stat 33 / resid 20 / hw 10 / gnss 12 / sensor 5 / comms 8 / failure 23.
11. **Frontend revert (Ahmed's explicit call)**: unpopulated-row hiding
    (battery + GPS) fully reverted — all rows render always, placeholders
    included. No hiding machinery remains.

---

## 7. Codebase state

`backend/{main.py, health_monitor.py, features.py, db.py, enums.py,
ros_live_source.py, extract_bag.py, requirements.txt, test_health_monitor.py,
test_persistence.py, data/*.json + manifest.json, dashboard.db}`,
`frontend/{app.js, index.html, style.css}`, `virtual_robot/{Dockerfile,
entrypoint.sh, start_ros_stack.sh, relay_node.py, speedtest_server.py,
msg_packages/}`, `scratch/{replay_harness.py, sweep.py, peek.py}`,
`ml/{train.py, artifacts/}`, `bag/2026-06-10-10-54-06.bag`,
`docker-compose.yml`. No `ml/infer.py`, no `verify_schema_assumptions.py`.
`docker compose up` never run in this environment; live re-discovery and
reconnect paths logic-reviewed but not end-to-end tested without rosbridge.
`features.py` docstring still pre-v6 prose (content correct). README reference
numbers + changelog current as of 2026-09-07.

---

## 8. Project B (Anaghim Knani's) — source-verified, reference only

Same robot/bag (message counts match). 4 ROS1 nodes, IF + PyTorch AE → ONNX,
React/TS dashboard. Confirmed defects to avoid: three incompatible feature
schemas in one repo (28 deployed / 13 `train_real.py` / 14 simulated);
`driver_fault` as model input (label leakage — this project reserves fault
as ground-truth-only); P5–P95 "normal" clipping (this project excludes the
confirmed window instead); random split (chronological + embargo here);
hardcoded `contamination=0.01` (quantile-targeted thresholds here);
`classify_anomaly()` branching on nonexistent feature names; report claims 26
features vs 28 in code. Reusable (with tuning, not blindly): θ = μ+3σ as a
starting convention. Direction unchanged: A stays the deterministic
foundation, B's ML approach (cleaned) fuses as category 9 — not a parallel
system. No failure-prediction claims without labeled failure history.

---

## 9. Immediate next actions

1. **Phase 3 — `ml/infer.py`** (all constraints locked, in `metadata.json`
   `phase3_constraints`): build 23-vector → `require_matching_schema()` →
   project to 13 ML features → `vdrop = raw supply − bat_voltage`
   (**NEVER `clean_supply_voltage()` on vdrop inputs** — §4a) → RobustScaler
   from `scaler.pkl` → IF single-row + AE 20-window scores → per-state
   thresholds + OR fusion + 2s persistence → category 9 of
   `health_monitor.py` taxonomy. AE window on CPU + ONNX parity verified.
2. Do not start Phase 3 until golden + fast suites pass in the new
   environment (`python -m unittest test_health_monitor test_persistence`;
   golden needs `PGUARD_GOLDEN=1`, ~3.5 min).
3. Phase 4: evaluation beyond one fault type needs more failure data —
   currently doesn't exist; say so explicitly rather than generalizing.
4. Open (needs Ahmed, not guessing): whether 2D-fix-as-healthy is
   operationally accepted (code now assumes yes); real-robot unknowns in §1.

**Whoever picks this up: re-read §0 first.**
