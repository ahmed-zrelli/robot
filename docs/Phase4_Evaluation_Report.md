# Phase 4 — Evaluation Report (2026-09-08)

Objective: prove with numbers what the ML adds over the deterministic
system, and state exactly where proof ends. Analysis only — no production
code changed except one cosmetic message fix found by the analysis itself
(see §6). Method artifacts: `scratch/events_A.json`, `scratch/events_B.json`
(full 1× replay halves with complete event records), `scratch/events_20x_A.json`,
`ml/artifacts/val_a.json` + `val_b.json` (per-tick validation records),
`ml/artifacts/threshold_curve.csv`. Halves split at t=6500 (post-fault idle
tail); seam refires (gnss:fix, motor_temperature dead flags active across
the split) treated as continuations; t∈[6500,6700] excluded from rate math.

## 1. Overlap: what ML caught vs the rules

5 `ml:anomaly` events over the full run. **Every one overlaps deterministic
events within ±30s — zero ML-only intervals.** Honest reading: on this bag
the ML is redundancy (independent mechanism, same events), not unique
coverage. "Catches what rules miss" is theory until a second fault type
exists (§7).

| t (bag s) | Models | Disposition |
|---|---|---|
| 1610.8 | IF+AE, idle | Real multivariate current excursion on all 4 motors; statistical flapping alongside. Corroborated transient, not novel. |
| 3054.0 | IF, idle | Pre-fault, overlaps statistical (RR motor_current). Corroborated. |
| 3126.6 | IF+AE (AE 38121), idle | Fault onset +1s; bitmask + failures + statistical alongside. Redundant path confirmed working. |
| 3404.5 | IF, idle | Post-driving-segs, fault still active; bitmask clear/new flapping + statistical alongside. |
| 3426.9 | IF, idle | Mid-fault; bitmask + supply_voltage statistical alongside. |

Fault-window coverage by kind (news): hardware_fault 10 (blip flapping),
statistical 3 (suppression working — the collapse no longer feeds baselines),
residual 0, gnss 1, ml 3, failure 13 (0s hw escalation per edge).

## 2. Ablation (assembled, not re-run)

| Model | Idle FPR | Drive FPR | Notes |
|---|---|---|---|
| IF-only | 1.15% / 1.15% (off/on) | 0.79% / 0.91% | Transfers online almost exactly |
| AE-only | — | — / 0.33% (stride-5) | 2.06% at stride-1: phase-sensitivity finding, fixed by eval-tick attribution |
| Fused | 1.23% / 1.06% | 1.26% / 1.28% | Whole-pool online: 1.09% pooled, 0.56 events/h |

(off = train gate, on = streaming validation.) Two models justified: IF
carries driving + immediacy (0.1s), AE carries sustained shape; neither
alone matches fused behavior across states.

## 3. Latency (measured, synthetic 5Hz ticks)

Full tick p50 11.3ms / p99 16.3ms / max 17.3ms vs the 200ms tick budget
(~6–8%, dominated by IF's 200 trees). Project B claims <5ms; ours is slower
and honest. `n_estimators` reduction is a *future retrain* option — the
validated model is not touched for speed.

## 4. Threshold sensitivity (`threshold_curve.csv`, monotone, no cliffs)

Quantile 0.99 → IF/AE test FPR ~1.8%/1.9%; 0.995 (operating) → ~1.0%;
0.999 → ~0.3% (recall cost unmeasured — do not chase without a second
fault to price it against). Operating point sits on a gentle slope:
re-tuning is neither needed nor justified with current data.

## 5. Replay-speed robustness (20×, half A)

Degrades gracefully and predictably: 29 events (vs 106 at 1×), ML fires
exactly once — new t=3166.9 (fault onset, coarser sampling delays it),
cleared t=4118.9. Transients vanish (undersampled), the sustained fault
survives. Same wall-clock distortion family as the deterministic checks
(README known-limitation): 1× is truth, ML judgement off fast replays is
demo-only. No code change — documented, same policy as deterministic.

## 6. Incidental fix

Overlap duty exposed a wrong message: non-eval ticks printed "AE warming"
3000s into a run. `MLScorer` now tracks first-eval (`ae_live`); messages
read warming / AE score / "–" (between evals) correctly.

## 7. Limits (hard boundaries, not modesty)

- Single fault type, single event: no generalization claim, no ML
  escalation window, no prediction. Ever, on this data.
- AE is an idle specialist (drive k≈13.4); drive quintile Q2 (t 1357–2024)
  reads 2.84% vs ≤1.5% elsewhere — unexplained heterogeneity, flagged not
  laundered.
- Drive val pool 970 rows; ONNX verified CPU-only (no GPU/ARM target).
- Golden ~15 min with ML wired: the price of future monitor changes.

## 8. Data wishlist (what unblocks the out-of-scope items)

1. A second, different failure mode (any) — unlocks generalization testing
   and the "novel fault" claim.
2. Sustained-failure examples with durations — unlocks ML escalation
   calibration (currently capability=None by design).
3. Labeled pre-failure history — the only thing that unlocks prediction;
   nothing else does.
4. A bag with real RTK variation — unlocks the carrierStatus/diffMode
   precision check (1/37,371 informative samples currently).
5. A charge cycle — unlocks charging-subsystem semantics.
