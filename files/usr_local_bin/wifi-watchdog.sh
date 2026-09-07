#!/bin/bash
# WiFi watchdog — runs every 5 minutes via cron.
# Detects WiFi disconnection and attempts automatic recovery:
#   Stage 1: restart NetworkManager
#   Stage 2: reboot (if NM restart didn't help within ~5 min)
#
# False-positive guards:
#   - If wlan0 has an IP and the gateway is reachable, but internet is down,
#     this is a DSL/internet issue — no action taken (don't restart NM for that).
#   - Reboot marker prevents repeat reboots within 1 hour.
#
# All status is logged to journal (logger -t wifi-watchdog).

set -uo pipefail

LOG_TAG="wifi-watchdog"
IFACE="wlan0"
GATEWAY="192.168.0.1"
INTERNET_HOST="1.1.1.1"
NM_RESTART_MARKER="/var/run/wifi-watchdog-nm-restarted"
REBOOT_MARKER="/var/run/wifi-watchdog-reboot-pending"
PING_OPTS="-c 2 -W 5"

log() {
    logger -t "$LOG_TAG" "$*"
}

ping_ok() {
    ping $PING_OPTS "$1" &>/dev/null
}

iface_has_ip() {
    ip addr show "$IFACE" 2>/dev/null | grep -q "inet "
}

# --- Fast path: internet reachable ---
if ping_ok "$INTERNET_HOST"; then
    rm -f "$NM_RESTART_MARKER" "$REBOOT_MARKER"
    exit 0
fi

# --- Internet not reachable ---

# Check if wlan0 has an IP at all
if ! iface_has_ip; then
    log "WARNING: $IFACE has no IP address (WiFi association lost)"
    wifi_state="no_ip"
elif ! ping_ok "$GATEWAY"; then
    log "WARNING: $IFACE has IP but gateway $GATEWAY unreachable (WiFi down or router rebooting)"
    wifi_state="no_gateway"
else
    # Has IP, gateway reachable, internet down → DSL/WAN issue, not a WiFi problem
    log "INFO: WiFi OK (gateway reachable) but internet unreachable — DSL/WAN issue, no action"
    rm -f "$NM_RESTART_MARKER"
    exit 0
fi

# --- WiFi is down (no IP or no gateway) ---

# Guard: don't reboot if we already rebooted recently
if [ -f "$REBOOT_MARKER" ]; then
    marker_age=$(( $(date +%s) - $(stat -c %Y "$REBOOT_MARKER" 2>/dev/null || echo 0) ))
    if [ "$marker_age" -lt 3600 ]; then
        log "WARNING: WiFi still down but reboot already pending (marker age: ${marker_age}s) — waiting"
        exit 1
    fi
    rm -f "$REBOOT_MARKER"
fi

# Stage 2: NM was restarted on the previous run and we're still down → reboot
if [ -f "$NM_RESTART_MARKER" ]; then
    marker_age=$(( $(date +%s) - $(stat -c %Y "$NM_RESTART_MARKER" 2>/dev/null || echo 0) ))
    if [ "$marker_age" -lt 600 ]; then
        log "CRITICAL: WiFi still down ${marker_age}s after NetworkManager restart — rebooting to recover"
        touch "$REBOOT_MARKER"
        rm -f "$NM_RESTART_MARKER"
        (sleep 10 && /sbin/reboot -f) &
        disown
        exit 1
    fi
    rm -f "$NM_RESTART_MARKER"
fi

# Stage 1: Restart NetworkManager
log "Attempting recovery: restarting NetworkManager (state: $wifi_state)"
touch "$NM_RESTART_MARKER"
systemctl restart NetworkManager
log "NetworkManager restarted — will verify on next run (~5 min)"
exit 0
