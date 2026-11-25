#!/bin/bash
set -e

# Speed-test target - independent lifecycle, up as soon as the container is.
python3 /opt/speedtest/speedtest_server.py &

# ROS stack auto-starts on container boot. Earlier versions required
# SSHing in and running start_robot manually, to mirror an unknown
# real-robot boot behavior - that distinction turned out not to matter:
# this container is a local dev/test convenience only (see README
# "Connecting to the real robot"), the dashboard's job is just to collect
# telemetry when it's available, and whatever the real robot's ROS stack
# does on boot is the robot side's concern, not something worth simulating
# here. Logged to a file rather than the container's stdout since it's
# backgrounded - see /var/log/ros_stack.log for roscore/rosbridge/relay
# output and bag-playback status.
echo "[virtual_robot] container up. Starting ROS stack automatically..."
/usr/local/bin/start_robot > /var/log/ros_stack.log 2>&1 &

echo "[virtual_robot] ROS stack starting in background - tail"
echo "[virtual_robot] /var/log/ros_stack.log for progress. rosbridge will be"
echo "[virtual_robot] reachable on :9090 within a few seconds."
echo "[virtual_robot] speed-test target already up on :8091 (/ping, /down, /up)."
echo "[virtual_robot]"
echo "[virtual_robot] Need a shell inside the container (rostopic/rosnode/"
echo "[virtual_robot] rosmsg, or to re-run start_robot manually)? Port 22 isn't"
echo "[virtual_robot] published to the host - use instead:"
echo "[virtual_robot]   docker compose exec virtual-robot bash"

exec /usr/sbin/sshd -D
