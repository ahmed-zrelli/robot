# PGuard Anomaly Detection — Handover v9 (Phase 3)

Date: 2026-09-08. Status: Phase 3 DONE and validated.

This file does NOT replace v8 — v8 (`PGuard_Handover_v8.md`, same folder)
remains the base record for project context, Phases 0–2.5, the bag
verification record, and Project B. This file records only what Phase 3
added/changed, so a fresh reader should read v8 first, then this.

**§0 of v8 still applies in full** (verify against code/data, run tests
after every edit, never tune to make golden pass).

---

## 1. Phase status delta vs v8

| Phase | v8 status | Now |
|---|---|---|
| 3 — `ml/infer.py` + category-9 fusion | Not started | **DONE, validated, wired, golden-pinned** |
| 4 — Evaluation beyond the gate | Not started | Not started (unchanged; still blocked on more failure data) |
| 5 — Frontend anomaly panel / alerts / export | Not started | Not started |

---

## 2. Phase 3 as built

**`ml/infer.py` — `MLScorer`** (standalone, no HealthMonitor dependency):
artifact load with the load-time schema gate; 14-field last-known cache over
5 topics (battery current/voltage + 4× motor_current/supply_current/raw
supply_voltage); 0.2s tick gate reusing the wall-clock-`t` convention;
2s per-field staleness gate (stale → skip tick, never score junk); vdrop
from RAW supply values (`clean_supply_voltage()` not imported by design);
sklearn IF every tick + onnxruntime AE every 5th tick (torch never imported
at inference); per-state idle/drive thresholds from `metadata.json`; OR
fusion with contributor list. Verdict dict per tick carries scores, flags,
driving state, AE-ready state. NaN/missing readings hold last-good within
max-age (never poison the cache); ticks gate on `/cmd`-derived driving
predicate identical to `HealthMonitor._is_driving`.

**Locked pre-Phase-3 decisions** (from review, all kept): onnxruntime
inference / torch training-only (parity 3.6e-06); single fused `ml:anomaly`
key; no failure escalation (`capability=None`); staleness on the 14 raw
fields only.

**Category-9 wiring** (`health_monitor.py::_check_ml`, docstring item 10):
only `ML_TOPICS` forwarded; shared 2s `_debounced_transition` persistence;
`kind="ml"`, warning, `capability=None` (excluded from escalation by
construction); contributor scores + driving state in the message. NOT
suppressed during hardware faults (frozen model cannot be contaminated; its
fault response is independent signal). Fail-soft: absent artifacts → ML off,
everything else untouched. `HealthMonitor` import of `ml.infer` is guarded;
`close()`/reconnect semantics unchanged (AE window rebuilds in ~4s, IF
immediate — warm-up replay into ML deliberately not built).

---

## 3. Findings during Phase 3 (all measured)

1. **Schema-gate key bug (real blocker, fixed)**: `require_matching_schema()`
   read `metadata.get("version")` while `train.py` writes `"schema_version"`
   — the gate could never pass against a real artifact (train.py never calls
   it, so Phase 2 numbers were unaffected). One-line fix in `features.py` +
   new `backend/test_schema_contract.py` (4 tests, zero heavy deps so it
   runs in minimal sandboxes without fastapi).
2. **No live 23-field vector exists.** The schema check compares declared
   metadata once at load; it never touches sensor values. The review's open
   question about "GNSS staleness for schema completeness" was built on a
   false premise and was dropped, not resolved — with the one-line rationale
   recorded so it isn't re-raised. Live ML needs exactly the 14 raw fields;
   GNSS/percentage/thermals/status never enter the path.
3. **AE stride finding (the validation story)**: first online version
   evaluated AE every tick and read AE-only 2.06% vs IF-only 0.91%/0.79%
   offline — the entire drive excess isolated to the AE by per-model split
   (shared machinery thereby exonerated). Cause: the MLP AE is sensitive to
   transient *phase* inside the flattened 20-sample window; stride-1
   evaluation samples ~5× more phases than the stride-5 calibration saw. A
   carried-verdict variant amplified firings ~5× and was reverted
   same-session. Final: AE evaluates every 5th tick with verdicts attributed
   to the eval tick only (mirrors train.py's window-to-last-row assignment).
   Apples-to-apples drive went 2.89% → **1.24% vs 1.26% offline**.
4. **Scope mismatch, confirmed then cleared**: offline drive TEST is t
   2797–3065 only (last 15% of drive rows); online covers all driving. The
   idle-match/drive-mismatch pattern itself pointed at scope first. After
   the stride fix the same-window comparison matches exactly, closing it.
5. **Mid-fault status blip (v8 §4b) interacts correctly**: hardware_fault
   edge-fires clear+new around the t≈3357.5 blip; ML sustained response
   spans it (flag dips inside driving sub-segments only, per the A-variant
   reporting).

## 4. Validation (pre-declared tolerances, all PASS)

`scratch/validate_infer.py` (split halves + `--combine`, per-tick records
for free re-slicing): pooled **1.09%** vs 1.24% gate (±0.5pp); idle 1.06 vs
1.23; drive 1.28 vs 1.26; delay **0.1s**; sustained 93.2/96.2 vs 93.0/96.0;
**0.56 events/h**. Cost note: ~63k ticks × ~10ms IF scoring ≈ 10–11 min
full-bag (fine live at 5Hz = ~6% of the 200ms budget; just volume). Halves
are independently re-runnable, combined exactly.

## 5. Tests + golden

- `backend/test_infer.py` (9): tick gating, staleness cutout, NaN
  hold-last-good, raw-vdrop tracking (sub-floor supply reaches vdrop 1:1),
  AE cold start + eval stride (~1.25s spacing), cmd driving flag,
  RAW_FIELDS↔`train.FEATURE_SRC` pin, wiring debounce + no-escalation +
  non-ML-topic silence (via injected fake scorer).
- Golden re-pinned with **`ml: 5`** (seven older keys byte-identical,
  confirming the wiring perturbed nothing); golden test now prints actual
  counts on mismatch so failures are diagnosable without a re-run.
  Full suite: **32 run + 1 golden, green** (golden ~15 min with ML wired,
  documented in its docstring).
- `scratch/replay_harness.py` gained progress output (shared benefit for
  all long replays).

## 6. Next actions (Phase 4/5, unchanged fundamentals)

1. Phase 4 needs more failure data before any generalization claim — still
   doesn't exist; the single UNDERVOLTAGE event cannot calibrate an ML
   escalation window or a broader recall number. Say so, don't extrapolate.
2. The two remaining quantitative warts, both documented, neither blocking:
   drive quintile Q2 (t 1357–2024) reads 2.84% vs ≤1.5% elsewhere (normal
   heterogeneity post-fix, but the fattest tail); AE remains an idle
   specialist (drive k≈13.4) with IF carrying driving.
3. Frontend Phase 5 (anomaly panel/alerts/export) untouched; Ahmed's
   all-rows-render policy stands.
4. `docker compose up` and live rosbridge end-to-end still never run in
   this environment — unchanged from v8.
