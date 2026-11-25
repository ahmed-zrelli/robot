"""Parameter sweep: DEBOUNCE_SECONDS x STALE_PERCENTILE x STALE_SCOPE x
playback speed, one config per process invocation (each full-bag replay
costs ~2.5 min).

Also measures the genuine-fault detection delay Ahmed asked about: for each
fired `new` event we record (fire_t - first_anomalous_sample_t), i.e. how
much real time the debounce added on top of the raw detection. Hardware/GNSS
events are edge-triggered and unaffected - their delay should be ~0.

SPEED: mirrors main.py::_stream_bag's actual pacing exactly - a message at
bag-time `t` is delivered at wall-clock `base + (t - first_t) / speed` (see
_stream_bag's `target_real_elapsed`), computed directly rather than actually
slept through. health.ingest() still always receives that wall-clock value,
same as production. A previous version of this script always used speed=1
regardless of what was passed to it (it never divided by speed at all) -
fixed here; rows written by that version are re-labeled speed=1.0 in the CSV
since that's what they actually measured, not backfilled/estimated.

Usage: python scratch/sweep.py <debounce> <percentile> <scope> [speed=1.0]
Appends one line to scratch/sweep_results.csv.
"""
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import health_monitor as hm
from health_monitor import HealthMonitor

DATA = Path(__file__).parent.parent / "backend" / "data"
RESULTS = Path(__file__).parent / "sweep_results.csv"


def load_all():
    manifest = json.load(open(DATA / "manifest.json"))
    merged = []
    for topic, fname in manifest["topics"].items():
        for r in json.load(open(DATA / fname)):
            merged.append((r["_t"], topic, r))
    merged.sort(key=lambda x: x[0])
    return merged


class Instrumented(HealthMonitor):
    def __init__(self):
        super().__init__()
        self._first_anom = {}   # key -> t of first anomalous sample of current run

    def _debounced_transition(self, key, is_anomalous, t):
        if is_anomalous:
            if not self._anom_since.get(key):
                # first crossing this run -> raw detection moment
                self._first_anom.setdefault(key, t)
        else:
            if key in self._active:
                pass  # keep _first_anom until cleared so fire_cleared isn't confused
            elif not self._anom_since.get(key):
                self._first_anom.pop(key, None)
        return super()._debounced_transition(key, is_anomalous, t)

    def emit_hook(self, ev):
        delay = None
        fa = self._first_anom.get(ev["key"])
        if fa is not None:
            delay = ev["started_at"] - fa
        return {"kind": ev["kind"], "signal": ev["signal"], "delay": delay}


def main():
    debounce, percentile, scope = float(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
    speed = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
    hm.DEBOUNCE_SECONDS = debounce
    hm.STALE_PERCENTILE = percentile
    hm.STALE_SCOPE = scope

    merged = load_all()
    mon = Instrumented()
    counts = {}
    delays = []
    base = time.time()
    first_t = merged[0][0]
    for t_rel, topic, data in merged:
        t = base + (t_rel - first_t) / speed
        for ev in mon.ingest(topic, data, t):
            if ev["state"] == "new":
                k = (ev["kind"], ev["signal"])
                counts[k] = counts.get(k, 0) + 1
                info = mon.emit_hook(ev)
                if info["delay"] is not None:
                    delays.append((info["kind"], info["delay"]))
    total = sum(counts.values())
    stat_total = sum(v for (k, _), v in counts.items() if k == "statistical")
    comms_total = sum(v for (k, _), v in counts.items() if k == "comms")
    resid_total = sum(v for (k, _), v in counts.items() if k == "residual")

    import statistics
    by_kind = {}
    for kind, d in delays:
        by_kind.setdefault(kind, []).append(d)
    delay_str = "; ".join(
        f"{k}: median {statistics.median(v):.1f}s max {max(v):.1f}s n={len(v)}"
        for k, v in sorted(by_kind.items()))

    new_file = not RESULTS.exists()
    with open(RESULTS, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["debounce", "percentile", "scope", "speed", "total", "statistical",
                        "comms", "residual", "gnss", "delay_summary"])
        w.writerow([debounce, percentile, scope, speed, total, stat_total, comms_total,
                    resid_total, counts.get(("gnss", "GPS / GNSS"), 0), delay_str])
    print(f"done deb={debounce} p={percentile} scope={scope} speed={speed}: total={total} "
          f"stat={stat_total} comms={comms_total} resid={resid_total}")
    print(delay_str)


if __name__ == "__main__":
    main()
