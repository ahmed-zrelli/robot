"""
One-off extraction script: reads the ROS1 .bag file and converts EVERY
topic found in it into a compact JSON timeline file used by the replay
server.

No topic whitelist. No per-topic hardcoded decimation. Drop a different
.bag in bag/ and this extracts whatever topics it actually contains -
nothing to edit here first.

Design decisions (documented, not hidden):
- All high-rate topics are decimated to a single flat display rate
  (DEFAULT_DISPLAY_HZ) since a browser number updating 50x/sec is
  unreadable anyway and would bloat the payload for no benefit. This is a
  *display* decimation, not a claim about the sensor's real rate. Topics
  publishing slower than that rate natively just keep every message.
- Timestamps are kept in seconds relative to the first message in the bag,
  so the replay server can scale playback speed cleanly.
- Any message type rosbags can't deserialize (unknown/corrupt msgdef) is
  skipped with a warning, not silently dropped - you'll see it in the
  extraction log and in manifest.json's "skipped_topics".
"""
import json
import sys
from pathlib import Path
from collections import defaultdict

from rosbags.rosbag1 import Reader
from rosbags.typesys import Stores, get_typestore, get_types_from_msg

PROJECT_ROOT = Path(__file__).parent.parent
BAG_DIR = PROJECT_ROOT / "bag"
OUT_DIR = Path(__file__).parent / "data"
OUT_DIR.mkdir(parents=True, exist_ok=True)

_bag_candidates = sorted(BAG_DIR.glob("*.bag"))
if not _bag_candidates:
    sys.exit(f"No .bag file found in {BAG_DIR}. Drop your rosbag there and re-run.")
BAG_PATH = str(_bag_candidates[0])
print(f"Using bag: {BAG_PATH}")

# Flat cap applied to every topic - no per-topic table. Raise/lower this one
# number if you want denser or sparser stored data.
DEFAULT_DISPLAY_HZ = 5


def msg_to_dict(msg):
    """Recursively convert a rosbags deserialized message into plain dict/list,
    dropping the ROS Header (we use the bag's own connection timestamp instead)."""
    if hasattr(msg, "__dataclass_fields__"):
        out = {}
        for field in msg.__dataclass_fields__:
            if field == "header":
                continue
            # skip ROS msg constants (ALL_CAPS class attrs) and internal
            # rosbags bookkeeping fields (__msgtype__ etc.) - only keep
            # actual per-instance data fields.
            if field.startswith("__") or field.isupper():
                continue
            val = getattr(msg, field)
            out[field] = msg_to_dict(val)
        return out
    elif isinstance(msg, (list, tuple)):
        return [msg_to_dict(v) for v in msg]
    elif isinstance(msg, bytes):
        return msg.decode(errors="replace")
    elif hasattr(msg, "tolist"):
        # numpy array (any size, including 0/1-element) -> plain python list
        return msg.tolist()
    else:
        return msg


def main():
    # Build the typestore entirely from the bag's own embedded message
    # definitions (source of truth for what's actually in this bag) rather
    # than a built-in ROS preset, which can differ (e.g. older BatteryState
    # field layout) and cause deserialization to fail.
    typestore = get_typestore(Stores.EMPTY)

    with Reader(BAG_PATH) as reader:
        print(f"Bag contains {len(reader.connections)} connections across "
              f"{len({c.topic for c in reader.connections})} topics:")
        for c in reader.connections:
            print(f"  {c.topic}  ({c.msgtype})  ~{c.msgcount} msgs")

        skipped_types = set()
        seen = set()
        for conn in reader.connections:
            if conn.msgtype in seen:
                continue
            seen.add(conn.msgtype)
            try:
                types = get_types_from_msg(conn.msgdef.data, conn.msgtype)
                typestore.register(types)
            except Exception as e:
                print(f"WARN: could not register {conn.msgtype}: {e}", file=sys.stderr)
                skipped_types.add(conn.msgtype)

        start_time = reader.start_time  # ns
        connections = [c for c in reader.connections if c.msgtype not in skipped_types]

        by_topic = defaultdict(list)
        topic_msgtype = {}
        last_kept_t = defaultdict(lambda: -1e9)
        min_dt = 1.0 / DEFAULT_DISPLAY_HZ

        count = 0
        deser_errors = defaultdict(int)
        for conn, timestamp, rawdata in reader.messages(connections=connections):
            count += 1
            if count % 200000 == 0:
                print(f"...{count} messages processed")

            t_rel = (timestamp - start_time) / 1e9
            if t_rel - last_kept_t[conn.topic] < min_dt:
                continue
            last_kept_t[conn.topic] = t_rel

            try:
                msg = typestore.deserialize_ros1(rawdata, conn.msgtype)
            except Exception as e:
                deser_errors[conn.topic] += 1
                if deser_errors[conn.topic] <= 1:
                    print(f"WARN: deserialize failed for {conn.topic} "
                          f"({conn.msgtype}): {e}", file=sys.stderr)
                continue

            d = msg_to_dict(msg)
            d["_t"] = round(t_rel, 3)
            by_topic[conn.topic].append(d)
            topic_msgtype[conn.topic] = conn.msgtype

        manifest_topics = {}
        for topic, msgs in by_topic.items():
            fname = topic.strip("/").replace("/", "_") + ".json"
            out_path = OUT_DIR / fname
            with open(out_path, "w") as f:
                json.dump(msgs, f)
            print(f"{topic}: {len(msgs)} messages -> {out_path} ({out_path.stat().st_size/1e6:.2f} MB)")
            manifest_topics[topic] = fname

        manifest = {
            "duration_s": (reader.end_time - reader.start_time) / 1e9,
            "topics": manifest_topics,
            "topic_msgtype": topic_msgtype,
            "skipped_topics": sorted(
                {c.topic for c in reader.connections if c.msgtype in skipped_types}
            ),
        }
        with open(OUT_DIR / "manifest.json", "w") as f:
            json.dump(manifest, f, indent=2)

        # clean up any leftover JSON from a previous bag's topics that no
        # longer exist in this one, so data/ never has stale files
        current_files = set(manifest_topics.values()) | {"manifest.json"}
        for f in OUT_DIR.glob("*.json"):
            if f.name not in current_files:
                print(f"Removing stale file from a previous extraction: {f.name}")
                f.unlink()

        print("done.")


if __name__ == "__main__":
    main()
