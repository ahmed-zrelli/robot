"""Enum/bitmask name mappings for roboteq + GNSS messages. Single source of
truth - main.py and health_monitor.py both import from here instead of each
keeping their own copy."""

FAULT_BITS = {
    1: "OVERHEAT", 2: "OVERVOLTAGE", 4: "UNDERVOLTAGE", 8: "SHORT_CIRCUIT",
    16: "EMERGENCY_STOP", 32: "SEPEX_EXCITATION_FAULT", 64: "MOSFET_FAILURE",
    128: "STARTUP_CONFIG_FAULT",
}
FIX_TYPES = {0: "NO_FIX", 1: "DEAD_RECKONING", 2: "2D", 3: "3D", 4: "GNSS+DR", 5: "TIME_ONLY"}
DIFF_SOURCES = {0: "NONE", 1: "SBAS", 2: "GBAS", 3: "RTK_WIDE", 4: "RTK_NARROW", 5: "SBAS_PPP"}
