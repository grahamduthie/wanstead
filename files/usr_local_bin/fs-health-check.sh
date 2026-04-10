#!/bin/bash
# Filesystem health check — runs every 5 minutes via cron.
# Detects read-only filesystem, attempts automatic recovery,
# reboots as last resort if recovery fails.
#
# All status is logged to journal (logger -t fs-health-check).

set -uo pipefail

LOG_TAG="fs-health-check"
TEST_FILE="/var/log/.fs_health_test"
SERVICES=("wcam-auth" "wcam-ws-relay" "ustreamer-logitech" "ustreamer-sweex" "nginx")
REBOOT_MARKER="/var/log/.fs_recovery_reboot_pending"

log() {
    logger -t "$LOG_TAG" "$*"
}

# --- SD Card Health Checks ---

check_sd_card_health() {
    # Check SD card health indicators. Returns 0=healthy, 1=warning, 2=critical.
    local status=0

    # Check for mmc0 errors in recent dmesg (last 1000 lines)
    local mmc_errors
    mmc_errors=$(dmesg -T 2>/dev/null | grep -c "mmc0.*\(error\|timeout\|reset\|CRC\)" || true)
    if [ "$mmc_errors" -gt 0 ]; then
        log "WARNING: $mmc_errors mmc0 errors found in dmesg"
        status=1
    fi

    # Check for I/O errors
    local io_errors
    io_errors=$(dmesg -T 2>/dev/null | grep -c "I/O error\|blk_update_request.*error\|Buffer I/O error" || true)
    if [ "$io_errors" -gt 0 ]; then
        log "CRITICAL: $io_errors I/O errors found in dmesg"
        status=2
    fi

    # Check filesystem state
    local fs_state
    fs_state=$(/usr/sbin/tune2fs -l /dev/mmcblk0p2 2>/dev/null | grep "Filesystem state:" | awk '{print $NF}')
    if [ -z "$fs_state" ]; then
        log "WARNING: Could not read filesystem state (tune2fs may have failed)"
        if [ "$status" -lt 1 ]; then status=1; fi
    elif [ "$fs_state" != "clean" ]; then
        log "CRITICAL: Filesystem state is '$fs_state' (expected 'clean')"
        status=2
    fi

    # Check mount count vs last check (warn if >180 days since last check)
    local mount_count last_check_str
    mount_count=$(/usr/sbin/tune2fs -l /dev/mmcblk0p2 2>/dev/null | grep "^Mount count:" | awk '{print $NF}')
    last_check_str=$(/usr/sbin/tune2fs -l /dev/mmcblk0p2 2>/dev/null | grep "^Last checked:" | sed 's/Last checked: *//')
    if [ -n "$last_check_str" ] && [ -n "$mount_count" ]; then
        local last_check_ts
        last_check_ts=$(date -d "$last_check_str" +%s 2>/dev/null || echo 0)
        local now_ts
        now_ts=$(date +%s)
        local days_since_check=$(( (now_ts - last_check_ts) / 86400 ))
        if [ "$days_since_check" -gt 180 ]; then
            log "WARNING: Filesystem not checked in $days_since_check days (mount count: $mount_count)"
            if [ "$status" -lt 1 ]; then status=1; fi
        fi
    fi

    echo "$status"
}

# --- Main Recovery Logic ---

# Step 1: Quick check — can we write to /var/log?
if touch "$TEST_FILE" 2>/dev/null && rm -f "$TEST_FILE" 2>/dev/null; then
    # Filesystem is writable — run SD card health check every run
    # (lightweight: just reads dmesg and tune2fs output)
    sd_status=$(check_sd_card_health)
    case "$sd_status" in
        0) ;; # Healthy — silent
        1) log "SD card health: WARNING — monitor for recurring issues" ;;
        2) log "SD card health: CRITICAL — consider replacing SD card" ;;
    esac
    exit 0
fi

log "WARNING: /var/log is read-only — attempting recovery"

# Check if we've already tried recovery too many times
if [ -f "$REBOOT_MARKER" ]; then
    MARKER_AGE=$(( $(date +%s) - $(stat -c %Y "$REBOOT_MARKER" 2>/dev/null || echo 0) ))
    if [ "$MARKER_AGE" -lt 3600 ]; then
        # Marker is less than 1 hour old — don't keep rebooting
        log "WARNING: Recovery already attempted recently, skipping (marker age: ${MARKER_AGE}s)"
        exit 1
    fi
    # Marker is old — remove it and try again
    rm -f "$REBOOT_MARKER"
fi

# Step 2: Try to remount the root filesystem as read-write
if mount -o remount,rw / 2>/dev/null; then
    log "SUCCESS: remounted / as read-write"

    # Verify it actually worked
    if touch "$TEST_FILE" 2>/dev/null && rm -f "$TEST_FILE" 2>/dev/null; then
        log "VERIFIED: filesystem is now writable — restarting services"
        for svc in "${SERVICES[@]}"; do
            if systemctl is-active --quiet "$svc" 2>/dev/null; then
                systemctl restart "$svc" 2>/dev/null && \
                    log "Restarted $svc" || \
                    log "WARNING: failed to restart $svc"
            fi
        done
        log "Recovery complete (remount)"
        exit 0
    fi
fi

# Step 3: Remount failed — try once more with a brief delay
log "WARNING: First remount attempt failed — retrying after 5s delay"
sleep 5
if mount -o remount,rw / 2>/dev/null; then
    log "SUCCESS: remounted / as read-write (second attempt)"
    if touch "$TEST_FILE" 2>/dev/null && rm -f "$TEST_FILE" 2>/dev/null; then
        rm -f "$REBOOT_MARKER"
        log "VERIFIED: filesystem is now writable — restarting services"
        for svc in "${SERVICES[@]}"; do
            if systemctl is-active --quiet "$svc" 2>/dev/null; then
                systemctl restart "$svc" 2>/dev/null && \
                    log "Restarted $svc" || \
                    log "WARNING: failed to restart $svc"
            fi
        done
        log "Recovery complete (remount, second attempt)"
        exit 0
    fi
fi

# Step 4: All recovery attempts failed — schedule a reboot as last resort
log "CRITICAL: All remount attempts failed — scheduling forced reboot in 60 seconds"
log "CRITICAL: If this recurs, the SD card may be failing and should be replaced"
touch "$REBOOT_MARKER" 2>/dev/null || true

# Give monitoring systems 60 seconds to detect the issue before rebooting
(sleep 60 && /sbin/reboot -f) &
disown

exit 1
