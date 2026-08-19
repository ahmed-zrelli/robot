#!/bin/bash
# Manually starts the "robot's" ROS stack.
#
# This is the step you SSH in and run yourself - it mirrors a real robot
# whose onboard software isn't set up to auto-start on boot. On the actual
# robot this might instead be `roslaunch some_package robot.launch` (ROS1)
# or `ros2 launch some_package robot.launch.py` (ROS2), or a systemd
# service someone manually starts/restarts - whatever it turns out to be,
# the *workflow* (SSH in, run a command, THEN the dashboard can connect)
# should feel the same as this.
set -e
source /opt/ros/noetic/setup.bash
source /catkin_ws/devel/setup.bash

BAG_FILE=$(find /bag -maxdepth 1 -name "*.bag" | head -n 1)
if [ -z "$BAG_FILE" ]; then
  echo "[start_robot] ERROR: no .bag file found in /bag - mount your bag folder there."
  exit 1
fi

echo "[start_robot] starting roscore..."
roscore &
sleep 3

echo "[start_robot] starting rosbridge websocket on :9090..."
roslaunch --wait rosbridge_server rosbridge_websocket.launch &
sleep 3

echo "[start_robot] playing (looping): $BAG_FILE"
echo "[start_robot] the dashboard can connect now."
echo "[start_robot] Ctrl+C here stops bag playback (roscore/rosbridge keep running)."
echo "[start_robot] tip: run this inside 'screen'/'tmux' or with nohup if you want it"
echo "[start_robot]      to survive closing this SSH session - same tradeoff you'll"
echo "[start_robot]      have on the real robot."
exec rosbag play --loop --clock "$BAG_FILE"
