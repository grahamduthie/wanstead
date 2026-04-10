#!/bin/bash
# Namecheap Dynamic DNS updater
# Requires NAMECHEAP_DDNS_PASSWORD environment variable (set in root crontab).
HOST="wansteadcam"
DOMAIN="lumoco.com"
PASSWORD="${NAMECHEAP_DDNS_PASSWORD:?ERROR: NAMECHEAP_DDNS_PASSWORD not set}"
STATE_FILE="/var/run/namecheap-ddns-ip"
LOG_FILE="/var/log/namecheap-ddns.log"

CURRENT_IP=$(curl -s --max-time 10 https://api.ipify.org)

if [ -z "$CURRENT_IP" ]; then
    echo "$(date): Failed to get public IP" >> "$LOG_FILE"
    exit 1
fi

if [ -f "$STATE_FILE" ]; then
    LAST_IP=$(cat "$STATE_FILE")
    if [ "$CURRENT_IP" = "$LAST_IP" ]; then
        exit 0
    fi
fi

RESPONSE=$(curl -s --max-time 10 "https://dynamicdns.park-your-domain.com/update?host=${HOST}&domain=${DOMAIN}&password=${PASSWORD}&ip=${CURRENT_IP}")

if echo "$RESPONSE" | grep -q "<ErrCount>0</ErrCount>"; then
    echo "$CURRENT_IP" > "$STATE_FILE"
    echo "$(date): Updated to $CURRENT_IP" >> "$LOG_FILE"
else
    echo "$(date): Update failed — $RESPONSE" >> "$LOG_FILE"
    exit 1
fi
