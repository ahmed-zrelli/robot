"""Offline replay harness: merges all extracted bag topic timelines by
timestamp, feeds them through HealthMonitor with synthetic wall-clock time
(no sleeping - a full 212-min bag replays in ~1-2 min). Used for A/B
parameter comparison only; not part of the dashboard app."""
import json
import sys
import time
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from health_monitor import HealthMonitor  # noqa: E402

DATA = Path(__file__).parent.parent / "backend" / "data"


def load_all(t0=0.0, t1=float("inf")):
    manifest = json.load(open(DATA / "manifest.json"))
    merged = []  # (t, topic, data)
    for topic, fname in manifest["topics"].items():
        rows = json.load(open(DATA / fname))
        for r in rows:
            if t0 <= r["_t"] < t1:
                merged.append((r["_t"], topic, r))
    merged.sort(key=lambda x: x[0])
    return merged


def replay(merged, speed=1.0, full=False):
    """Simulates main.py::_stream_bag's actual pacing model WITHOUT real
    sleeping: a message at bag-time `t` is delivered at real wall-clock
    `base + (t - first_t) / speed` (see _stream_bag's `target_real_elapsed`),
    and health.ingest() always receives that wall-clock value - exactly like
    the deployed app, just computed instead of waited for. This is what lets
    `speed` actually affect HealthMonitor's WINDOW_SECONDS/DEBOUNCE_SECONDS
    behavior (which are wall-clock), rather than the old version of this
    function which fed 1:1 bag-time-as-wall-clock regardless of the `speed`
    argument name suggesting otherwise.

    full=False (default): events_log entries are (t_rel, state, kind,
    signal) tuples - the shape TestGoldenReplay pins. full=True: entries
    are dicts with the complete event including message (ML contributor
    scores live there) - for Phase 4 overlap/characterization analysis.
    Deterministic either way: same input gives byte-identical output."""
    t0 = time.time()
    mon = HealthMonitor()
    counts = {}
    events_log = []
    base = t0  # synthetic wall-clock anchor
    first_t = merged[0][0]
    n = len(merged)
    for i, (t_rel, topic, data) in enumerate(merged):
        if i % 100000 == 0:
            print(f"  replay {i}/{n} msgs ({time.time()-t0:.0f}s elapsed)",
                  flush=True)
        t = base + (t_rel - first_t) / speed
        for ev in mon.ingest(topic, data, t):
            k = (ev["kind"], ev["signal"])
            if ev["state"] == "new":
                counts[k] = counts.get(k, 0) + 1
            if full:
                events_log.append({"t": round(t_rel, 3),
                                   "state": ev["state"],
                                   "kind": ev["kind"],
                                   "key": ev.get("key", ""),
                                   "signal": ev["signal"],
                                   "message": ev.get("message", "")})
            else:
                events_log.append((t_rel, ev["state"], ev["kind"],
                                   ev["signal"]))
    elapsed = time.time() - t0
    return counts, events_log, elapsed


def summarize(counts, elapsed):
    total_new = sum(counts.values())
    print(f"  new events: {total_new}  (replay took {elapsed:.0f}s)")
    for k in sorted(counts, key=counts.get, reverse=True):
        print(f"    {counts[k]:5d}  {k[0]:16s} {k[1]}")


if __name__ == "__main__":
    speed = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
    dump = sys.argv[2] if len(sys.argv) > 2 else None
    # Optional halves for long runtimes (each ~7 min instead of ~15):
    # argv[3], argv[4] = t0, t1 bag-time window. State restarts cold at
    # t0, so treat events within ~200s after a nonzero t0 as warm-up
    # artifacts, not signal. The t=6500 seam sits in the post-fault idle
    # tail, far from every Phase 4 question (fault window, driving
    # transients); analyses exclude [6500, 6700] explicitly.
    t0 = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
    t1 = float(sys.argv[4]) if len(sys.argv) > 4 else float("inf")
    print("loading...")
    merged = load_all(t0, t1)
    dur = merged[-1][0] - merged[0][0]
    print(f"{len(merged)} messages, {dur/60:.0f} min bag, speed={speed}x")
    counts, log, el = replay(merged, speed=speed, full=bool(dump))
    summarize(counts, el)
    if dump:
        with open(dump, "w") as f:
            json.dump(log, f)
        print(f"wrote {len(log)} full events to {dump}")
