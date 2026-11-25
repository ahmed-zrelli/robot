"""
Live telemetry source - subscribes to a real ROS1 system over rosbridge
(the virtual robot container, or later the real robot).

Topic list and message types are discovered from the robot ITSELF at
connect time, via ROS's own introspection service (rosapi, exposed by
rosbridge). This file has zero dependency on backend/data/ or anything
extracted locally - if you point ROSBRIDGE_HOST at a robot running a
completely different bag/dataset, this adapts with no code change.

Used by main.py when TELEMETRY_SOURCE=live. Outbound shape to the frontend
is unchanged: {type:'msg', topic, t, data} - same contract as bag replay.

Some topics can't be subscribed to directly: the publisher's actual message
layout doesn't match what's compiled/installed in this rosbridge (a real
ROS version/era mismatch - e.g. sensor_msgs/BatteryState gained fields
between when this bag was recorded and what the container has installed),
or the message's package isn't installed here at all (e.g. nmea_msgs -
rosbridge can't even resolve the type to attempt a subscribe). Both are
covered by the same fix, applied uniformly to every topic rather than a
per-topic list - see virtual_robot/relay_node.py's docstring for the full
root-cause writeup on both failures.

relay_node.py (running inside the virtual_robot container) subscribes to
every topic with rospy.AnyMsg - which skips both failure modes entirely -
decodes each message dynamically from the publisher's own live field
layout (no compiled message package needed, nothing hardcoded,
self-adapting if the robot's fields or topic list change later), and
republishes the data as JSON text on /relay<original topic> (std_msgs/
String - a type that can never hit either failure). This file detects
those /relay/... topics at discovery time, subscribes to them instead of
the raw (potentially-doomed) native topic, and re-presents the data to
on_message()/on_topics_discovered() under its ORIGINAL topic name -
main.py and everything downstream never know a relay is involved.
"""
import json
import time
import roslibpy

RELAY_PREFIX = "/relay"


class RosLiveSource:
    """Wraps a roslibpy connection. on_message(topic, t, data) is called
    from roslibpy's own background thread for every incoming message.
    on_topics_discovered(list of (topic, type)) is called once per
    connect() - but only after discover_topics_stable() confirms the
    topic set has stopped changing, not off a single early snapshot.
    After that, poll_new_topics() (called periodically by main.py) picks
    up topics that appear mid-session without reconnecting."""

    def __init__(self, host="localhost", port=9090):
        self.client = roslibpy.Ros(host=host, port=port)
        self._subs = []
        self._t0 = None
        self._on_message = None
        self._subscribed_topics = set()  # raw discovered names already subscribed
        self._raw_subs = {}              # original topic -> raw roslibpy.Topic

    def discover_topics(self, timeout=5.0):
        """Ask the robot what topics currently exist, via rosapi. Returns
        a list of (topic, type) - live, from the robot, not from any file."""
        service = roslibpy.Service(self.client, "/rosapi/topics", "rosapi/Topics")
        request = roslibpy.ServiceRequest()
        result = {}

        def _cb(response):
            result.update(response)

        service.call(request, _cb, errback=lambda e: result.update({"error": str(e)}))

        waited = 0.0
        while not result and waited < timeout:
            time.sleep(0.05)
            waited += 0.05

        if "error" in result:
            raise ConnectionError(f"rosapi/topics call failed: {result['error']}")
        if not result:
            raise ConnectionError("rosapi/topics call timed out - is rosapi running on the robot?")

        topics = result.get("topics", [])
        types = result.get("types", [])
        return list(zip(topics, types))

    def discover_topics_stable(self, poll_timeout=5.0, stable_window=1.0, max_wait=15.0):
        """Repeatedly calls discover_topics() until the returned topic set
        stops changing for `stable_window` seconds, or `max_wait` elapses -
        whichever comes first.

        Exists because a single rosapi/topics query, taken the instant
        rosbridge becomes reachable, can land mid-startup on the publisher
        side (relay_node.py still discovering/subscribing to raw topics,
        or rosbag play not yet running) and return an incomplete topic
        list. Since discovery in connect() only happens once per session
        (see class docstring), a snapshot taken too early used to mean
        that whole session stayed stuck at whatever partial/empty list it
        got - only a full reconnect (running discovery again, later) would
        pick up the rest. Polling until the set is stable fixes this
        without guessing a fixed startup delay: it naturally adapts to
        however long relay_node.py/rosbag actually take, on this run,
        under whatever load the machine is under - not a hardcoded sleep.
        """
        start = time.monotonic()
        last_set = None
        last_change = start
        result = []
        while True:
            result = self.discover_topics(timeout=poll_timeout)
            current_set = frozenset(result)
            now = time.monotonic()
            if current_set != last_set:
                last_set = current_set
                last_change = now
            if now - last_change >= stable_window:
                break
            if now - start >= max_wait:
                break
            time.sleep(0.3)
        return result

    def _subscribe_relay(self, topic, on_message):
        """Subscribe to a /relay/<original> JSON topic, re-presented under
        the original name. If a raw subscription for the same data already
        exists (topic appeared before its relay did), drop it so each
        message isn't delivered twice."""
        original = topic[len(RELAY_PREFIX):]
        old = self._raw_subs.pop(original, None)
        if old is not None:
            try:
                old.unsubscribe()
            except Exception:
                pass
            if old in self._subs:
                self._subs.remove(old)
        sub = roslibpy.Topic(self.client, topic, "std_msgs/String")

        def relay_handler(msg, original=original):
            try:
                data = json.loads(msg["data"])
            except (KeyError, ValueError) as e:
                print(f"[ros_live_source] bad relay payload on {original}: {e}")
                return
            on_message(original, round(time.monotonic() - self._t0, 3), data)

        sub.subscribe(relay_handler)
        self._subs.append(sub)

    def _subscribe_raw(self, topic, msg_type, on_message):
        sub = roslibpy.Topic(self.client, topic, msg_type)

        def handler(msg, topic=topic):
            on_message(topic, round(time.monotonic() - self._t0, 3), msg)

        sub.subscribe(handler)
        self._subs.append(sub)
        self._raw_subs[topic] = sub

    def poll_new_topics(self):
        """One rosapi poll; subscribes to anything not subscribed yet.

        Safe to call repeatedly from an executor thread - main.py calls it
        every ~30s so topics appearing mid-session (node restart, late
        publisher, bag loop restart) get picked up without reconnecting.
        Returns effective [(topic, type)] under ORIGINAL names for the
        caller to forward as a meta_update. Never raises: a failed poll
        just returns [] and tries again next round."""
        if self._on_message is None:
            return []
        try:
            discovered = self.discover_topics()
        except Exception as e:
            print(f"[ros_live_source] re-discovery poll failed: {e}")
            return []
        types_by_topic = dict(discovered)
        relayed_originals = {
            t[len(RELAY_PREFIX):]
            for t, _ in discovered
            if t.startswith(RELAY_PREFIX + "/")
        }
        fresh = []
        for topic, msg_type in discovered:
            if topic in self._subscribed_topics:
                continue
            self._subscribed_topics.add(topic)
            if topic.startswith(RELAY_PREFIX + "/"):
                original = topic[len(RELAY_PREFIX):]
                self._subscribe_relay(topic, self._on_message)
                fresh.append((original, types_by_topic.get(original, "unknown")))
            elif topic in relayed_originals:
                # A relay is handling this topic under /relay/<topic> -
                # the raw subscription would just hang on the same
                # md5 mismatch or missing-package error relay_node.py
                # exists to work around.
                continue
            else:
                self._subscribe_raw(topic, msg_type, self._on_message)
                fresh.append((topic, msg_type))
        return fresh

    def connect(self, on_message, on_topics_discovered=None, timeout=5.0):
        self.client.run(timeout=timeout)
        if not self.client.is_connected:
            raise ConnectionError(f"Could not reach rosbridge at {self.client.host}:{self.client.port}")
        self._t0 = time.monotonic()
        self._on_message = on_message

        discovered = self.discover_topics_stable(poll_timeout=timeout)
        types_by_topic = dict(discovered)

        # original_topic -> True for every topic that has a /relay/<topic>
        # counterpart currently discovered (i.e. relay_node.py is actively
        # handling it). These get skipped on the raw/native path below.
        relayed_originals = {
            t[len(RELAY_PREFIX):]
            for t, _ in discovered
            if t.startswith(RELAY_PREFIX + "/")
        }

        if on_topics_discovered:
            # Present relayed topics under their ORIGINAL name/type here too,
            # so PANEL_MAP and topics_meta in main.py never see "/relay/...".
            effective = [
                (t, mt) for t, mt in discovered
                if not t.startswith(RELAY_PREFIX + "/") and t not in relayed_originals
            ]
            effective += [
                (orig, types_by_topic.get(orig, "unknown"))
                for orig in relayed_originals
            ]
            on_topics_discovered(effective)

        for topic, msg_type in discovered:
            self._subscribed_topics.add(topic)
            if topic.startswith(RELAY_PREFIX + "/"):
                self._subscribe_relay(topic, on_message)
                continue
            if topic in relayed_originals:
                # A relay is handling this topic under /relay/<topic> above -
                # the raw subscription here would just hang on the same
                # md5 mismatch or missing-package error relay_node.py
                # exists to work around.
                continue
            self._subscribe_raw(topic, msg_type, on_message)

    def close(self):
        for sub in self._subs:
            try:
                sub.unsubscribe()
            except Exception:
                pass
        self._subs.clear()
        self._subscribed_topics.clear()
        self._raw_subs.clear()
        self._on_message = None
        if self.client.is_connected:
            self.client.close()
