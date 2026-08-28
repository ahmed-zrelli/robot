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


def load_all():
    manifest = json.load(open(DATA / "manifest.json"))
    merged = []  # (t, topic, data)
    for topic, fname in manifest["topics"].items():
        rows = json.load(open(DATA / fname))
        for r in rows:
            merged.append((r["_t"], topic, r))
    merged.sort(key=lambda x: x[0])
    return merged


def replay(merged, speed=1.0):
    """Simulates main.py::_stream_bag's actual pacing model WITHOUT real
    sleeping: a message at bag-time `t` is delivered at real wall-clock
    `base + (t - first_t) / speed` (see _stream_bag's `target_real_elapsed`),
    and health.ingest() always receives that wall-clock value - exactly like
    the deployed app, just computed instead of waited for. This is what lets
    `speed` actually affect HealthMonitor's WINDOW_SECONDS/DEBOUNCE_SECONDS
    behavior (which are wall-clock), rather than the old version of this
    function which fed 1:1 bag-time-as-wall-clock regardless of the `speed`
    argument name suggesting otherwise."""
    t0 = time.time()
    mon = HealthMonitor()
    counts = {}
    events_log = []
    base = t0  # synthetic wall-clock anchor
    first_t = merged[0][0]
    for t_rel, topic, data in merged:
        t = base + (t_rel - first_t) / speed
        for ev in mon.ingest(topic, data, t):
            k = (ev["kind"], ev["signal"])
            if ev["state"] == "new":
                counts[k] = counts.get(k, 0) + 1
            events_log.append((t_rel, ev["state"], ev["kind"], ev["signal"]))
    elapsed = time.time() - t0
    return counts, events_log, elapsed


def summarize(counts, elapsed):
    total_new = sum(counts.values())
    print(f"  new events: {total_new}  (replay took {elapsed:.0f}s)")
    for k in sorted(counts, key=counts.get, reverse=True):
        print(f"    {counts[k]:5d}  {k[0]:16s} {k[1]}")


if __name__ == "__main__":
    speed = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
    print("loading...")
    merged = load_all()
    dur = merged[-1][0] - merged[0][0]
    print(f"{len(merged)} messages, {dur/60:.0f} min bag, speed={speed}x")
    counts, log, el = replay(merged, speed=speed)
    summarize(counts, el)
