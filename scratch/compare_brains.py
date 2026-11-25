"""Brain-vs-brain fault comparison. Offline from dumped event JSONs.
Ground truth: the UNDERVOLTAGE window 3125.6-4004.4s (fault byte 4, both
sides) + the mid-window status blip at ~3357.5s. That is the ONLY labeled
fault in the bag (n=1) - so "who caught more" is answered as coverage
quality + delay + behavior at edges, not as a score. Run: python
scratch/compare_brains.py (seconds)."""
import json
from pathlib import Path

SCRATCH = Path(__file__).resolve().parent
FAULT_START, FAULT_END = 3125.6, 4004.4
FIRST_FAULT_SAMPLE = 3125.631


def load():
    evs = []
    for name in ("events_A.json", "events_B.json"):
        with open(SCRATCH / name) as f:
            evs += json.load(f)
    evs.sort(key=lambda e: e["t"])
    return evs


def show(title, evs):
    print(f"\n=== {title} ===")
    for e in evs:
        print(f"  t={e['t']:9.1f}  {e['state']:7s}  {e['signal']}")


def active_fraction(evs, kind, t0, t1):
    """Fraction of [t0,t1] with any active (uncleared) event of kind.
    Pre-window history sets the starting level (events already active at
    t0 count from t0); transitions outside the window otherwise ignored."""
    trans = sorted(
        [(e["t"], 1) for e in evs if e["kind"] == kind and e["state"] == "new"]
        + [(e["t"], -1) for e in evs
            if e["kind"] == kind and e["state"] == "cleared"])
    level, prev, active = 0, t0, 0.0
    for tt, d in trans:
        if tt < t0:
            level = max(0, level + d)
            continue
        if tt > t1:
            break
        if level > 0:
            active += tt - prev
        level = max(0, level + d)
        prev = tt
    if level > 0:
        active += t1 - prev
    return active / (t1 - t0)


def main():
    evs = load()
    hw = [e for e in evs if e["kind"] == "hardware_fault"]
    ml = [e for e in evs if e["kind"] == "ml"]
    show("BRAIN 1 - hardware_fault, full run", hw)
    show("BRAIN 2 - ml:anomaly, full run", ml)

    hw_first = min((e["t"] for e in hw if e["state"] == "new"),
                   default=None)
    ml_first = min((e["t"] for e in ml if e["state"] == "new"
                    and FAULT_START <= e["t"] <= FAULT_END), default=None)
    print("\n=== first detection of the labeled fault ===")
    print(f"  brain 1 (bitmask): t={hw_first:.1f} "
          f"(+{hw_first - FIRST_FAULT_SAMPLE:.1f}s after first fault sample)")
    print(f"  brain 2 (ML)     : t={ml_first:.1f} "
          f"(+{ml_first - FIRST_FAULT_SAMPLE:.1f}s after first fault sample)")

    print("\n=== sustained coverage of the fault window ===")
    for kind in ("hardware_fault", "ml"):
        print(f"  {kind:14s} active {active_fraction(evs, kind, FAULT_START, FAULT_END)*100:.1f}% of window")

    print("\n=== ground-truth solidity: nonzero-fault fraction per 100s bin ===")
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                           / "backend"))
    import json as _json
    ddir = Path(__file__).resolve().parent.parent / "backend" / "data"
    for side, fname in (("left", "left_roboteq_driver_status.json"),
                        ("right", "right_roboteq_driver_status.json")):
        rows = _json.loads((ddir / fname).read_text())
        bins = {}
        for r in rows:
            b = int(r["_t"] // 100) * 100
            n, k = bins.get(b, (0, 0))
            bins[b] = (n + 1, k + (1 if r.get("fault", 0) else 0))
        line = " ".join(f"{b}:{k/n*100:.0f}%"
                        for b, (n, k) in sorted(bins.items())
                        if 3000 <= b <= 4100 and n > 0)
        print(f"  {side}: {line}")

    print("\n=== outside-window fault claims (should be none for bitmask) ===")
    out_hw = [e for e in hw if not (FAULT_START <= e["t"] <= FAULT_END)]
    out_ml = [e for e in ml if not (FAULT_START <= e["t"] <= FAULT_END)]
    print(f"  brain 1 outside-window hardware_fault events: {len(out_hw)}")
    for e in out_hw:
        print(f"    t={e['t']:.1f} {e['state']} {e['signal']}")
    print(f"  brain 2 outside-window ml events: {len(out_ml)} (transients, "
          f"see Phase 4 dispositions)")
    for e in out_ml:
        if e["state"] == "new":
            print(f"    t={e['t']:.1f} {e['message']}")


if __name__ == "__main__":
    main()
