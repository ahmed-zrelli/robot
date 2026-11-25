"""Phase 4 overlap + characterization analysis. Offline from the dumped
event JSONs (halves merged) - seconds to run, re-runnable freely.

Seam handling: half B restarts state cold at t=6500, so "new" events in B
for keys active at end of A (gnss:fix, the four motor_temperature dead
flags) are continuations, not new detections. They are merged back into
the ongoing interval for overlap purposes and excluded from rate math.
Analyses additionally exclude t in [6500, 6700] (cold-baseline warm-up).

Usage: python scratch/analyze_overlap.py
"""
import json
from pathlib import Path

SCRATCH = Path(__file__).resolve().parent
FAULT = (3125.6, 4004.4)
SEAM = (6500.0, 6700.0)

KEY_OF = lambda e: (e["kind"], e["signal"])


def load():
    evs = []
    for name in ("events_A.json", "events_B.json"):
        with open(SCRATCH / name) as f:
            evs += json.load(f)
    evs.sort(key=lambda e: e["t"])
    return evs


def intervals(evs):
    """key -> list of (start, end|None) active intervals."""
    iv, open_at = {}, {}
    for e in evs:
        k = KEY_OF(e)
        if e["state"] == "new":
            open_at.setdefault(k, e["t"])
        elif e["state"] == "cleared" and k in open_at:
            iv.setdefault(k, []).append((open_at.pop(k), e["t"]))
    for k, t in open_at.items():
        iv.setdefault(k, []).append((t, None))
    return iv


def main():
    evs = load()
    print(f"{len(evs)} events total")
    iv = intervals(evs)

    ml_news = [e for e in evs if e["kind"] == "ml" and e["state"] == "new"]
    print(f"\n=== {len(ml_news)} ml:anomaly events ===")
    for e in ml_news:
        print(f"  t={e['t']:9.1f}  {e['message']}")
        near = [x for x in evs
                if x["kind"] != "ml" and abs(x["t"] - e["t"]) <= 30.0]
        kinds = sorted({(x["kind"], x["signal"], x["state"]) for x in near})
        for k in kinds:
            print(f"      +-{k[2]:7s} {k[0]:12s} {k[1]}")

    print("\n=== fault-window coverage by kind ===")
    for kind in ("hardware_fault", "statistical", "residual", "gnss",
                 "ml", "failure"):
        cov = [e for e in evs if e["kind"] == kind
               and FAULT[0] <= e["t"] <= FAULT[1]]
        news = sum(1 for e in cov if e["state"] == "new")
        span = (min((e["t"] for e in cov), default=None),
                max((e["t"] for e in cov), default=None))
        print(f"  {kind:14s} news={news:3d} "
              f"span={span[0] and f'{span[0]:.0f}-{span[1]:.0f}' or '-'}")

    print("\n=== ML-only intervals (no deterministic event within +-30s) ===")
    det = [e for e in evs if e["kind"] != "ml"]
    for e in ml_news:
        if not any(abs(x["t"] - e["t"]) <= 30.0 for x in det):
            print(f"  t={e['t']:.1f} ML ALONE: {e['message']}")
    print("(none printed above = every ML event overlaps deterministic ones)")

    print("\n=== seam check (t in [6500,6700] excluded from rates) ===")
    seam = [e for e in evs if SEAM[0] <= e["t"] <= SEAM[1]]
    print(f"  {len(seam)} events in seam window")


if __name__ == "__main__":
    main()
