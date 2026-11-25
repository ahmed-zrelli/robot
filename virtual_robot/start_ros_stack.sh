#!/bin/bash
# Starts the virtual robot's ROS stack (roscore, rosbridge, relay, bag
# playback). Auto-run once by entrypoint.sh on container start; also
# installed as `start_robot` on PATH so it can be re-run manually over SSH
# (e.g. to restart bag playback without rebuilding/restarting the
# container).
set -e
source /opt/ros/noetic/setup.bash
source /catkin_ws/devel/setup.bash

BAG_FILE=$(find /bag -maxdepth 1 -name "*.bag" | head -n 1)
if [ -z "$BAG_FILE" ]; then
  echo "[start_robot] ERROR: no .bag file found in /bag - mount your bag folder there."
  echo "[start_robot] contents of /bag:"
  ls -la /bag 2>&1
  exit 1
fi

echo "[start_robot] starting roscore..."
roscore &
sleep 3

echo "[start_robot] starting rosbridge websocket on :9090..."
roslaunch --wait rosbridge_server rosbridge_websocket.launch &
sleep 3

# Relays every topic through a decode path that works regardless of
# md5sum mismatches (e.g. /battery/state) or missing message packages
# (e.g. /nmea_sentence) - see relay_node.py's docstring for the full
# root-cause writeup. No per-topic config: it discovers topics itself
# and picks up ones added later automatically.
echo "[start_robot] starting type-agnostic relay..."
python3 /opt/relay/relay_node.py &
sleep 1

echo "[start_robot] playing (looping): $BAG_FILE"
echo "[start_robot] the dashboard can connect now."
echo "[start_robot] Ctrl+C here stops bag playback (roscore/rosbridge keep running)."
echo "[start_robot] tip: run this inside 'screen'/'tmux' or with nohup if you want it"
echo "[start_robot]      to survive closing this SSH session - same tradeoff you'll"
echo "[start_robot]      have on the real robot."
exec rosbag play --loop --clock "$BAG_FILE"
