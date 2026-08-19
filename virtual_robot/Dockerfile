# Virtual robot: plays back the recorded bag as live ROS1 topics and
# exposes them over rosbridge (WebSocket, JSON) so the dashboard backend
# can subscribe exactly like it would to the real robot later.
FROM ros:noetic-ros-core

# Fetch prerequisites (curl/gnupg2) using Ubuntu's own already-signed repos,
# before we touch the ROS apt repo at all.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg2 ca-certificates && rm -rf /var/lib/apt/lists/*

# The official ros-core image points apt at snapshots.ros.org (a pinned
# snapshot archive, used for build reproducibility). That server is
# unreliable/unreachable on a lot of networks. Separately, the ROS archive's
# signing key baked into this image has since expired/rotated - as of
# writing, apt reports it's missing key F42ED6FBAB17C654. Repoint at the
# live ROS package server AND fetch the current signing key before
# installing anything from it.
RUN . /etc/os-release && \
    echo "deb http://packages.ros.org/ros/ubuntu ${UBUNTU_CODENAME} main" \
    > /etc/apt/sources.list.d/ros1-latest.list && \
    curl -sSL 'https://keyserver.ubuntu.com/pks/lookup?op=get&search=0xF42ED6FBAB17C654' \
    | apt-key add -
    # If keyserver.ubuntu.com is blocked on your network, the alternative
    # source ROS itself publishes is:
    # curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.asc | apt-key add -

RUN apt-get update && apt-get install -y --no-install-recommends \
    ros-noetic-rosbridge-server \
    ros-noetic-catkin \
    ros-noetic-rosapi \
    python3-catkin-tools \
    build-essential \
    openssh-server \
    && rm -rf /var/lib/apt/lists/*

# SSH access - so you can log into the "robot" like a real remote machine
# and run rostopic/rosnode/rosmsg yourself, without the dashboard project
# telling you in advance what's there.
RUN mkdir /var/run/sshd && \
    useradd -m -s /bin/bash robot && \
    echo "robot:robot" | chpasswd && \
    echo "source /opt/ros/noetic/setup.bash" >> /home/robot/.bashrc && \
    echo "source /catkin_ws/devel/setup.bash" >> /home/robot/.bashrc && \
    ssh-keygen -A
EXPOSE 22

# Build the two real msg packages (roboteq_msgs, enova_msgs) so rosbridge
# can introspect and serialize them to JSON. motor_controller_msgs is
# deliberately excluded - unused by this bag.
RUN mkdir -p /catkin_ws/src
COPY msg_packages/roboteq_msgs /catkin_ws/src/roboteq_msgs
COPY msg_packages/enova_msgs /catkin_ws/src/enova_msgs

WORKDIR /catkin_ws
RUN /bin/bash -c "source /opt/ros/noetic/setup.bash && catkin_make"

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Manual-start command - run this after SSHing in, not auto-run on boot.
# See start_ros_stack.sh for why (mirrors a robot that must be launched
# manually rather than one whose software auto-starts).
COPY start_ros_stack.sh /usr/local/bin/start_robot
RUN chmod +x /usr/local/bin/start_robot

# rosbridge websocket
EXPOSE 9090

ENTRYPOINT ["/entrypoint.sh"]
