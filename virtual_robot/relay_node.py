#!/usr/bin/env python3
"""
type_agnostic_relay - works around ROS1's normal, typed rosbridge
subscribe path failing for a topic, for ANY reason: an md5sum mismatch
(the publisher's actual message layout doesn't match what's installed
here), or the message's package not being installed/buildable in this
container at all. Relays EVERY topic it finds, generically - no per-
topic allowlist, so a topic added to the robot later shows up
automatically with no code/config change here.

Two concrete failures this was built from:

  1. /battery/state - md5sum mismatch. This container's installed ROS
     Noetic sensor_msgs/BatteryState has 15 fields (includes temperature,
     cell_temperature[]). Whatever recorded this bag was running an older
     sensor_msgs/BatteryState with 13 fields. ROS1 refuses to connect a
     publisher/subscriber pair whose md5sums (computed from field layout)
     don't match - at the TCPROS handshake level, before any message
     content is transferred. rosbridge's own log:
       [ERROR]: Client [/rosbridge_websocket] wants topic /battery/state
       to have datatype/md5sum [sensor_msgs/BatteryState/4ddae7f0...],
       but our version has [sensor_msgs/BatteryState/476f837f...].
       Dropping connection.
     (`rostopic hz` still worked against this same topic because it uses
     looser subscription semantics than a full typed subscribe - what
     echo/rospy.wait_for_message/rosbridge all require.)

  2. /nmea_sentence - missing package entirely. rosbridge's normal
     subscribe path resolves a message type by importing its Python
     package; nmea_msgs isn't installed in this container, so it can't
     even get that far:
       [ERROR]: subscribe: Unable to load the manifest for package
       nmea_msgs. Caused by: nmea_msgs
     This has nothing to do with a version mismatch - the package is
     just absent - but it's the same underlying limitation: rosbridge's
     normal path needs the type resolvable/matching HERE, on this
     container, for every topic. Any future topic can hit either failure
     mode (or a new one) for reasons that have nothing to do with the
     dashboard.

How this node avoids both, for every topic it discovers:
  1. Subscribe with rospy.AnyMsg - this SKIPS both the md5sum check and
     the "resolve+import a message class" step entirely, so the
     subscription succeeds regardless of version mismatch OR a missing
     package.
  2. On every message, ROS provides a TCPROS connection header including
     `message_definition` - the PUBLISHER's OWN, live, plaintext .msg
     definition, exactly as it actually is right now. Nothing here reads
     that from a compiled package or any local file.
  3. genpy.dynamic.generate_dynamic(type_name, message_definition) builds
     a real, working message class at runtime from that text alone - no
     compiled/installed message package needed for this type, ever.
  4. Deserialize the raw bytes with that dynamically-generated class,
     convert to a plain dict (msg_to_dict below - same convention as
     backend/extract_bag.py's msg_to_dict: drop `header`, flatten
     everything else), and republish as JSON text on
     /relay<original topic> using std_msgs/String - a type that can
     never hit a md5 mismatch or a missing-package error, so rosbridge
     can always subscribe to it.

Which topics get relayed: ALL of them, discovered by periodically
polling the ROS master (rospy.get_published_topics()) rather than a
fixed list - so a topic that appears on the robot later (after this
node has already started) gets picked up on the next poll with no
restart, no code change, no config. The only topics deliberately
excluded are this node's own /relay/* output (to avoid relaying a
relay).

Zero invented/mimicked data: every field in the output comes from the
publisher's own declared layout at that moment - nothing is hardcoded,
guessed, or backfilled. If a message fails to decode, it's logged and
skipped, never silently guessed at.

Run standalone (sources ROS env the same way start_ros_stack.sh does):
    python3 relay_node.py
"""
import json
import os
import hashlib
import threading

import rospy
import genpy.dynamic
from std_msgs.msg import String

RELAY_PREFIX = "/relay"
POLL_INTERVAL_S = float(os.environ.get("RELAY_POLL_INTERVAL_S", "3"))

# Topics never relayed: our own output (would relay a relay), plus ROS
# log/introspection chatter (/rosout log aggregation, /rosapi services)
# that the dashboard has no use for and that only costs bandwidth.
# /diagnostics is deliberately NOT skipped - aggregated diagnostics may
# become a health source later.
SKIP_PREFIXES = (RELAY_PREFIX + "/", "/rosout", "/rosapi")

# Per-topic cache of the dynamically-generated class, keyed by topic.
# Rebuilt automatically if the publisher's declared type OR definition ever
# changes underneath us (e.g. robot software update): keyed on the hash of
# the definition text itself, not just the type name, since the same type
# name with a different field layout is exactly the md5-mismatch failure
# mode this node exists to survive.
_dynamic_classes = {}
_subscribed = set()  # topics we've already started relaying
_lock = threading.Lock()


def msg_to_dict(msg):
    """Recursively convert a genpy message (or nested field) into plain
    dict/list. Mirrors backend/extract_bag.py's msg_to_dict so live and
    bag data land in the identical shape - drops `header`, same as bag
    extraction."""
    if hasattr(msg, "__slots__"):
        out = {}
        for field in msg.__slots__:
            if field == "header":
                continue
            out[field] = msg_to_dict(getattr(msg, field))
        return out
    elif isinstance(msg, (list, tuple)):
        return [msg_to_dict(v) for v in msg]
    elif isinstance(msg, bytes):
        return msg.decode(errors="replace")
    elif hasattr(msg, "tolist"):  # numpy array, if genpy ever hands us one
        return msg.tolist()
    else:
        return msg


def _get_dynamic_class(topic, type_name, msg_definition):
    fingerprint = hashlib.sha256(msg_definition.encode()).hexdigest()
    cached = _dynamic_classes.get(topic)
    if cached and cached[0] == (type_name, fingerprint):
        return cached[1]
    classes = genpy.dynamic.generate_dynamic(type_name, msg_definition)
    cls = classes[type_name]
    _dynamic_classes[topic] = ((type_name, fingerprint), cls)
    rospy.loginfo(f"[relay] built dynamic class for {topic} ({type_name})")
    return cls


def make_callback(topic, pub):
    def _cb(anymsg):
        try:
            hdr = anymsg._connection_header
            type_name = hdr["type"]
            msg_definition = hdr["message_definition"]
            cls = _get_dynamic_class(topic, type_name, msg_definition)
            decoded = cls()
            decoded.deserialize(anymsg._buff)
            data = msg_to_dict(decoded)
            pub.publish(String(data=json.dumps(data)))
        except Exception as e:
            # Never crash the node over one bad message - log and move on,
            # same "don't silently assume, document the quirk" convention
            # as extract_bag.py's deserialize-error handling.
            rospy.logwarn(f"[relay] {topic}: failed to decode/republish - {e}")
    return _cb


def _relay_new_topics(_event=None):
    """Poll the ROS master for whatever topics currently exist and start
    relaying any we haven't already subscribed to. This - not just the
    startup pass - is what makes a topic added to the robot later show up
    automatically, with no restart and no code change."""
    try:
        published = rospy.get_published_topics()  # list of [topic, type]
    except Exception as e:
        rospy.logwarn(f"[relay] could not query master for topics: {e}")
        return

    with _lock:
        for topic, _type in published:
            if topic in _subscribed or topic.startswith(SKIP_PREFIXES):
                continue
            relay_topic = RELAY_PREFIX + topic
            pub = rospy.Publisher(relay_topic, String, queue_size=10)
            rospy.Subscriber(topic, rospy.AnyMsg, make_callback(topic, pub))
            _subscribed.add(topic)
            rospy.loginfo(f"[relay] relaying {topic} -> {relay_topic}")


def main():
    rospy.init_node("type_agnostic_relay", anonymous=False)
    _relay_new_topics()  # catch whatever's already publishing immediately
    rospy.Timer(rospy.Duration(POLL_INTERVAL_S), _relay_new_topics)
    rospy.loginfo(f"[relay] up - relaying all topics, watching for new ones every {POLL_INTERVAL_S}s")
    rospy.spin()


if __name__ == "__main__":
    main()
