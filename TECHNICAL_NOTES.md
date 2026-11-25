# Technical notes — why the system works this way

Design reasoning for the PGuard monitoring stack. Facts live in code
docstrings and handovers; this file keeps the *reasons* in one place so
future changes don't accidentally unlearn them. Each section names the
file that owns the decision.

## 1. One clock: wall time everywhere (`health_monitor.py`, `ml/infer.py`)

Windows, debounce, tick gates, staleness, escalation — all measured in
wall-clock seconds from a single `t` passed into `ingest()`. Never sample
counts (a count means different real durations at 1Hz vs 50Hz), never
bag-time (restarts every replay). Consequence, accepted deliberately:
replaying faster than 1× *widens* every window in bag-time terms (180s wall
= ~1h of bag at 20×), so detection behavior shifts with speed. Validate at
1×, demo at any speed. The ML tick gate inherits the same distortion —
same policy, no second exception carved.

## 2. Baselines are rolling, split, and maneuver-aware (`health_monitor.py`)

Three false-positive wars, in the order they were fought and won:
- **Maneuver suppression.** A commanded turn spikes currents/voltages on
  every motor — physics, not fault. Command steps above threshold excuse
  current/voltage judgement for a settling window; excused samples aren't
  fed to baselines either. Accepted cost: a real fault starting mid-maneuver
  hides until the window passes. Hardware faults bypass it (already confirmed
  reports need no debounce).
- **Idle/driving split.** Sustained driving elevates load for as long as it
  lasts, not just at maneuver instants — one mixed baseline is wrong for both
  states. Each field keeps two pools, gated on any nonzero commanded
  velocity (no hand-picked speed threshold). Measured 98 segments, median
  0.7s — the split matters *because* driving is fragmented, not despite it.
- **Quantization floor.** Flat-at-one-quantization-step windows have ~zero
  variance, so one ordinary step scores astronomical z. Fix: estimate each
  field's own step size (median nonzero gap) and apply Sheppard's q²/12 as
  a variance floor. No per-field constants invented.

## 3. Never learn the fault (`health_monitor.py` item 9, `ml/train.py`)

Fault samples in a learning baseline teach it that faults are normal, then
poison judgement for a full window after clearing (measured: 244 → 33
statistical events once fixed). So: split pools pause while a side's fault
bit is set; ML training excludes the window ±60s embargo. The frozen ML
model is exempt from suppression at inference — it *can't* learn, so its
fault response is signal (96% sustained, 0.1s delay), not contamination.

## 4. Dead sensors: suspect vs proven (`health_monitor.py` item 6)

Flatambiguous (idle settling? quantization? publish-on-change? dead link?)
gets `sensor_health`: warning, never escalates. Only a registered
physically-impossible constant (today: motor_temperature −4.1754 on all
four motors, std 0.0 across 3.5h + 9,406 live rows) earns `sensor_failure`:
critical, capability `supervision`, immediate escalation. Rule: warnings on
suspicion, failures only on proof — where "proof" means no healthy sensor
can produce the value, and the impaired capability is named honestly
(supervision, never borrowed mobility/power).

Judgement runs on stride-sampled pools (1/s + time eviction) because capped
rolling pools cover ~18s at 50Hz native rates and the 90s span could never
fill live — measured as phantom new→clear 17s after every connect.

## 5. Comms: each topic judges itself (`health_monitor.py` item 7)

No hardcoded expected rates (topics genuinely differ): staleness = quiet
longer than a high percentile of the topic's *own* gap history, floored at
5s. Publish-on-change topics (`/cmd`) get QUIET-not-STALE treatment plus a
residual cross-check (silence is expected if the motor tracks its static
command). Comms never escalates: silence proves no capability impaired
(wrong link? quiet robot?) — but stays critical-severity because a dead
link deserves eyes fast. Evaluated only on message arrival (no background
timer): total simultaneous silence waits for the next message anywhere.

## 6. Severity ≠ escalation (the two axes)

Severity (color) = how loudly to look. Escalation (failure event) = a
capability-impairment claim with a duration. Red that never becomes failure
(comms, suspect sensors) is not a bug — it's "urgent but unproven," which
is most of monitoring life. Collapsing the axes would manufacture failure
claims from ambiguous evidence; the dashboard groups co-active events into
incidents at display time instead (presentation interprets, detectors never
negotiate).

## 7. ML: stationary features, calibrated thresholds (`ml/train.py`)

- **vdrop differentials** (`supply − battery`): all six absolute voltage
  channels sag ~0.35V over a run (discharge physics) vs 0.18V idle noise —
  fixed models on absolutes measured 26% FPR on late data. Differentials
  cancel common-mode drift (0.04V residual) while the fault reads −45V.
  Same reason thermals/SoC stay rule-based: slow levels belong to adaptive
  baselines, fast multivariate shapes belong to the model.
- **Thresholds by val-set quantile** (0.5% per model → ~1% OR-fusion,
  measured jointly), reported as implied k for convention compatibility.
  Fixed k=3 and `contamination=` constants are the same sin in different
  clothes; MinMax scaling is rejected (two extreme points own every
  column) for RobustScaler.
- **Chronological splits + embargo + seeded determinism** throughout; random
  shuffling at 5Hz leaks near-duplicates across boundaries.
- **IF scores rows, AE scores 20-sample windows at stride 5** — and online
  evaluation matches that stride exactly, because the MLP AE is
  phase-sensitive (where a transient sits in the flattened window):
  stride-1 evaluation inflated AE-only FPR 4× (2.06% vs 0.5%), stride-5
  matches offline to 0.02pp. The carried-verdict variant that amplified
  firings 5× is recorded as a failure so nobody retries it.
- **What it is:** detection of the present (fastest honest signal: 0.1s).
  Not prediction — nothing here looks ahead, and the single labeled fault
  forbids generalization claims.

## 8. Inference plumbing (`ml/infer.py`)

No live 23-field vector exists — schema validation compares declared
metadata once at load. The live path is 14 raw fields over 5 topics in a
last-known cache with a 2s staleness gate (skip the tick, never score
junk); NaN holds last-good inside the gate. IF scores from tick one; AE
joins after 20 ticks (~4s cold start). OR fusion; 2s persistence downstream.
onnxruntime only (torch stays training-only; parity verified 3.6e-06).
Tick cost p50 11ms/p99 16ms vs the 200ms budget.

## 9. Ingestion robustness (the unglamorous layer that saves demos)

- **Relay** (`virtual_robot/relay_node.py`): subscribes `rospy.AnyMsg`,
  decodes from each publisher's live connection header via `genpy.dynamic`
  — immune by construction to md5/package mismatches that kill typed
  subscribes (the BatteryState variant that started this). Skips its own
  output plus ros* chatter; JSON String transport (note: unsuitable for
  future LiDAR pointclouds — plan binary then).
- **NaN policy**: sanitize once at the process boundary (`_sanitize`),
  because Python emits bare `NaN` tokens that aren't valid JSON and that
  FastAPI's strict encoder 500s on. Same bug class bit GPS rows, ML
  metadata, and would bite any new float source — hence boundary rule.
- **Persistence**: commits batched (200 rows/0.5s) because per-row commits
  stall the event loop at replay rates; bag telemetry decimated to
  1 row/topic/2s (full fidelity already in `backend/data/`), live recorded
  whole (the DB *is* the durable record there); warm-up reads pooled
  history so reconnects don't start blind (stats + residuals + non-gated
  liveness; split pools and maneuver history stay honestly cold).
- **Frontend trust**: robot-supplied strings escaped before `innerHTML`
  (topic names come from discovery); bag/live modes share one wire format
  so the UI never branches on source.
