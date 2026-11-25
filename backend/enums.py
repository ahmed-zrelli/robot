"""Enum/bitmask name mappings for roboteq + GNSS messages. Single source of
truth - main.py and health_monitor.py both import from here instead of each
keeping their own copy."""

FAULT_BITS = {
    1: "OVERHEAT", 2: "OVERVOLTAGE", 4: "UNDERVOLTAGE", 8: "SHORT_CIRCUIT",
    16: "EMERGENCY_STOP", 32: "SEPEX_EXCITATION_FAULT", 64: "MOSFET_FAILURE",
    128: "STARTUP_CONFIG_FAULT",
}
# roboteq_msgs/Status.msg `status` bitmask - separate bit space from FAULT_BITS
# above (same message, different byte). Bit 64 is intentionally absent: the
# .msg file defines 1/2/4/8/16/32/128 only and skips 64.
STATUS_BITS = {
    1: "SERIAL_MODE", 2: "PULSE_MODE", 4: "ANALOG_MODE", 8: "POWER_STAGE_OFF",
    16: "STALL_DETECTED", 32: "AT_LIMIT", 128: "MICROBASIC_SCRIPT_RUNNING",
}
FIX_TYPES = {0: "NO_FIX", 1: "DEAD_RECKONING", 2: "2D", 3: "3D", 4: "GNSS+DR", 5: "TIME_ONLY"}
DIFF_SOURCES = {0: "NONE", 1: "SBAS", 2: "GBAS", 3: "RTK_WIDE", 4: "RTK_NARROW", 5: "SBAS_PPP"}
# enova_msgs/GnssStatus.msg `carrierStatus` - RTK carrier-phase solution status
CARRIER_STATUS = {0: "NONE", 1: "FLOAT", 2: "FIXED"}
# sensor_msgs/BatteryState `power_supply_health` / `power_supply_technology`
# enum values (standard ROS message constants, not robot-specific)
POWER_SUPPLY_HEALTH = {
    0: "Unknown", 1: "Good", 2: "Overheat", 3: "Dead", 4: "Overvoltage",
    5: "Unspecified failure", 6: "Cold", 7: "Watchdog timer expired",
    8: "Safety timer expired",
}
POWER_SUPPLY_TECHNOLOGY = {
    0: "Unknown", 1: "NiMH", 2: "Li-ion", 3: "LiPo", 4: "LiFe", 5: "NiCd", 6: "LiMn",
}
