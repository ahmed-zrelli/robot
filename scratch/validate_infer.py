"""Offline validation for ml/infer.py: streams backend/data/*.json through
MLScorer in timestamp order (bag-time as wall-clock, i.e. 1x) and checks
the Phase 2 gate numbers reproduce in streaming/online form.

Expected differences vs train.py's batch gate (declared in advance, not
excuses after the fact): train.py scored the TEST split with merge_asof
nearest-match + ffill resampling and stride-5 AE windows; here assembly is
last-known-value with 2s staleness gating and stride-1 AE windows. So:
point-FPR should match within +-0.5pp, detection delay within seconds,
sustained fraction within a few pp. Anything outside that means the online
path diverges from what was validated - stop and investigate, do not
re-tune thresholds here.

Slow (~10 min full-bag: ~63k ticks x ~10ms IF scoring each - fine live at
5Hz, just volume): split with --t0/--t1 into halves, --out each half's
summary JSON, then --combine. Metric recombination is exact (counts, not
averages). The split point must keep the whole fault window (3125-4004s)
inside one half; 6500s does that.

Usage:
  python scratch/validate_infer.py --t0 0 --t1 6500 --out val_a.json
  python scratch/validate_infer.py --t0 6500 --t1 99999 --out val_b.json
  python scratch/validate_infer.py --combine val_a.json val_b.json
"""
import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "ml"))
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from infer import MLScorer  # noqa: E402

DATA = PROJECT_ROOT / "backend" / "data"
FAULT_START, FAULT_END = 3125.6, 4004.4
EMBARGO = 60.0
DRIVE_SEGS = [(3374.105, 3378.008), (3378.304, 3378.705), (3379.004, 3402.21)]

META = json.loads((PROJECT_ROOT / "ml" / "artifacts"
                   / "metadata.json").read_text())
GATE = META["test_gate"]


def load_merged(t0, t1):
    manifest = json.loads((DATA / "manifest.json").read_text())
    merged = []
    for topic, fname in manifest["topics"].items():
        for r in json.loads((DATA / fname).read_text()):
            if t0 <= r["_t"] < t1:
                merged.append((r["_t"], topic, r))
    merged.sort(key=lambda x: x[0])
    return merged


def in_drive_seg(t):
    return any(a <= t <= b for a, b in DRIVE_SEGS)


def run_half(t0, t1):
    t_start = time.time()
    merged = load_merged(t0, t1)
    print(f"[{t0}-{t1}] {len(merged)} messages loaded "
          f"({time.time()-t_start:.0f}s)", flush=True)
    scorer = MLScorer()
    normal_flags = {"idle": [0, 0], "drive": [0, 0]}  # state -> [flags, n]
    fw = []  # (t, driving, fused) inside the fault window
    nticks = []  # (t, driving, fused) normal pool: per-tick record so any
    # time-window slicing (apples-to-apples vs train splits, quintile
    # breakdowns, event clustering) is free without re-running the replay
    n_msg = n_tick = 0
    for t_rel, topic, data in merged:
        n_msg += 1
        if n_msg % 100000 == 0:
            print(f"[{t0}-{t1}] {n_msg}/{len(merged)} msgs, {n_tick} ticks, "
                  f"{time.time()-t_start:.0f}s elapsed", flush=True)
        v = scorer.update(topic, data, t_rel)
        if v is None:
            continue
        n_tick += 1
        if FAULT_START <= v["t"] <= FAULT_END:
            fw.append((v["t"], v["driving"], v["fused"]))
        elif not (FAULT_START - EMBARGO <= v["t"] <= FAULT_END + EMBARGO):
            s = "drive" if v["driving"] else "idle"
            normal_flags[s][0] += v["fused"]
            normal_flags[s][1] += 1
            nticks.append((round(v["t"], 1), v["driving"], v["if_flag"],
                           v["ae_flag"], v["fused"]))
    print(f"[{t0}-{t1}] done: {n_tick} ticks, "
          f"{time.time()-t_start:.0f}s total", flush=True)
    return {"normal_flags": normal_flags, "fw": fw, "nticks": nticks}


def cmd_run(args):
    summary = run_half(args.t0, args.t1)
    with open(args.out, "w") as f:
        json.dump(summary, f)
    print(f"wrote {args.out}", flush=True)


def cmd_combine(args):
    tot = {"idle": [0, 0], "drive": [0, 0]}
    fw = []
    nticks = []
    for path in args.combine:
        with open(path) as f:
            part = json.load(f)
        for s in tot:
            tot[s][0] += part["normal_flags"][s][0]
            tot[s][1] += part["normal_flags"][s][1]
        fw += [tuple(x) for x in part["fw"]]
        nticks += [tuple(x) for x in part.get("nticks", [])]
    fw.sort()
    # nticks schema: v1 (t, driving, fused) or v2 (t, driving, if, ae,
    # fused). Old half-files stay usable for counts.
    nticks = [tuple(x) for x in nticks]
    if nticks and len(nticks[0]) == 3:
        nticks = [(t, d, None, None, fl) for t, d, fl in nticks]
    n = tot["idle"][1] + tot["drive"][1]
    f = tot["idle"][0] + tot["drive"][0]
    fpr_all = f / n
    fpr_idle = tot["idle"][0] / tot["idle"][1]
    fpr_drive = tot["drive"][0] / tot["drive"][1]
    det = [t for t, _, fl in fw if fl]
    delay = (det[0] - 3125.631) if det else float("nan")
    sust = sum(fl for _, _, fl in fw) / len(fw) if fw else float("nan")
    nonmotion = [(t, d, fl) for t, d, fl in fw if not in_drive_seg(t)]
    sust_nm = (sum(fl for _, _, fl in nonmotion) / len(nonmotion)
               if nonmotion else float("nan"))
    g = GATE["point_fpr_fused_clean"]
    print(f"online point-FPR: pooled {fpr_all*100:.2f}%  "
          f"idle {fpr_idle*100:.2f}%  drive {fpr_drive*100:.2f}%")
    print(f"train gate was : pooled {g['pooled']*100:.2f}%  "
          f"idle {g['idle']*100:.2f}%  drive {g['drive']*100:.2f}%")
    print(f"fault window ({len(fw)} ticks): delay {delay:.1f}s "
          f"(gate {GATE['fault_detection_delay_s']:.1f}s), "
          f"sustained {sust*100:.1f}% "
          f"(gate {GATE['fault_sustained_frac']*100:.1f}%), "
          f"excl-driving {sust_nm*100:.1f}% "
          f"(gate {GATE['fault_sustained_frac_excl_driving']*100:.1f}%)")
    ok = (abs(fpr_all - g["pooled"]) <= 0.005
          and (fw == [] or abs(delay - GATE["fault_detection_delay_s"])
               <= 5.0)
          and (fw == [] or abs(sust_nm - GATE["fault_sustained_frac_excl_driving"])
               <= 0.05))
    print("VALIDATION", "PASS" if ok else "REVIEW")

    # -- apples-to-apples: online drive FPR restricted to train.py's drive
    # TEST window (t 2797-3065, the pool behind the 1.26% offline number)
    # plus drive FPR by time quintile (locates early-vs-late character
    # differences) plus sustained-event clustering (the events/h figure).
    # Per-model split (IF-only vs AE-only) isolates which model drives any
    # online excess; entries with None flags come from v1 half-files.
    if nticks:
        a2a = [(t, d, fi, fa, fl) for t, d, fi, fa, fl in nticks
               if d and 2797.0 <= t <= 3065.0]
        if a2a:
            print(f"apples-to-apples drive [2797-3065]: "
                  f"{sum(fl for _, _, _, _, fl in a2a)/len(a2a)*100:.2f}% "
                  f"over {len(a2a)} ticks (offline fused: "
                  f"{g['drive']*100:.2f}%)")
            scored = [(fi, fa) for _, _, fi, fa, _ in a2a
                      if fi is not None]
            if scored:
                gi = GATE["point_fpr_if"]
                print(f"  IF-only online: "
                      f"{sum(fi for fi, _ in scored)/len(scored)*100:.2f}% "
                      f"(offline IF drive: {gi['drive']*100:.2f}%)")
                print(f"  AE-only online: "
                      f"{sum(fa for _, fa in scored)/len(scored)*100:.2f}%")
        dticks = sorted((t, fl) for t, d, _, _, fl in nticks if d)
        q = max(1, len(dticks) // 5)
        for i in range(5):
            sl = dticks[i*q:(i+1)*q] if i < 4 else dticks[4*q:]
            if sl:
                print(f"  drive quintile {i+1} [t {sl[0][0]:.0f}-{sl[-1][0]:.0f}]: "
                      f"{sum(fl for _, fl in sl)/len(sl)*100:.2f}% "
                      f"({len(sl)} ticks)")
        # sustained runs >= 2s (10 ticks @5Hz) anywhere in the normal pool
        events, run = 0, 0
        prev_t = None
        for t, _d, _fi, _fa, fl in nticks:
            if fl and (prev_t is None or t - prev_t <= 1.0):
                run += 1
            else:
                if run >= 10:
                    events += 1
                run = 1 if fl else 0
            prev_t = t
        if run >= 10:
            events += 1
        span_h = (nticks[-1][0] - nticks[0][0]) / 3600 if nticks else 0
        print(f"sustained events(>=2s): {events} over {span_h:.2f}h "
              f"-> {events/span_h:.2f}/h")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--t0", type=float, default=0.0)
    ap.add_argument("--t1", type=float, default=99999.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--combine", nargs="+", default=None)
    args = ap.parse_args()
    if args.combine:
        cmd_combine(args)
    else:
        assert args.out, "--out required without --combine"
        cmd_run(args)
