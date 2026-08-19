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
"""
import time
import roslibpy


class RosLiveSource:
    """Wraps a roslibpy connection. on_message(topic, t, data) is called
    from roslibpy's own background thread for every incoming message.
    on_topics_discovered(list of (topic, type)) is called once, right
    after discovery, before any subscriptions are made."""

    def __init__(self, host="localhost", port=9090):
        self.client = roslibpy.Ros(host=host, port=port)
        self._subs = []
        self._t0 = None

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

    def connect(self, on_message, on_topics_discovered=None, timeout=5.0):
        self.client.run(timeout=timeout)
        if not self.client.is_connected:
            raise ConnectionError(f"Could not reach rosbridge at {self.client.host}:{self.client.port}")
        self._t0 = time.monotonic()

        discovered = self.discover_topics(timeout=timeout)
        if on_topics_discovered:
            on_topics_discovered(discovered)

        for topic, msg_type in discovered:
            sub = roslibpy.Topic(self.client, topic, msg_type)

            def handler(msg, topic=topic):
                on_message(topic, round(time.monotonic() - self._t0, 3), msg)

            sub.subscribe(handler)
            self._subs.append(sub)

    def close(self):
        for sub in self._subs:
            try:
                sub.unsubscribe()
            except Exception:
                pass
        self._subs.clear()
        if self.client.is_connected:
            self.client.terminate()
