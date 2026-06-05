#!/bin/bash
# Detects a hung wlan0 and recovers by restarting the interface.
# If the interface restart doesn't restore connectivity within 30s, reboots.
GATEWAY=172.16.10.1
IFACE=wlan0
LOGFILE=/var/log/network-watchdog.log

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOGFILE"; }

if ping -c 2 -W 5 "$GATEWAY" > /dev/null 2>&1; then
    exit 0
fi

log "Gateway unreachable — restarting $IFACE"
ip link set "$IFACE" down
sleep 5
ip link set "$IFACE" up
sleep 30

if ping -c 2 -W 5 "$GATEWAY" > /dev/null 2>&1; then
    log "Recovery successful"
    exit 0
fi

log "Recovery failed — rebooting"
/sbin/reboot
