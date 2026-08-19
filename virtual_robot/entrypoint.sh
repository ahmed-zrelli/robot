#!/bin/bash
set -e

echo "[virtual_robot] container up. ROS stack is NOT auto-started -"
echo "[virtual_robot] this mirrors a robot whose software has to be launched"
echo "[virtual_robot] manually rather than coming up on boot."
echo "[virtual_robot]"
echo "[virtual_robot] SSH in and start it yourself:"
echo "[virtual_robot]   ssh robot@localhost -p 2222   (password: robot)"
echo "[virtual_robot]   start_robot"
echo "[virtual_robot]"
echo "[virtual_robot] the dashboard's Connect button will fail/timeout until"
echo "[virtual_robot] you've done that - same as it would against a real robot"
echo "[virtual_robot] that hasn't been started yet."

exec /usr/sbin/sshd -D
