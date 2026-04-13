#!/usr/bin/env python3
"""Reboot the Netgear D7000 router.

Method: Fetch reboot.htm to get a dynamic form ID, then POST to setup.cgi.
Confirmed working on firmware V1.0.1.84_1.0.1 (April 2026).

Credentials are read from environment variables:
    ROUTER_IP        — router IP (default: 192.168.0.1)
    ROUTER_USER      — router admin username (default: admin)
    ROUTER_PASS      — router admin password (required)

Usage:
    sudo /usr/local/bin/reboot-router.py [--dry-run]
"""
import argparse
import logging
import os
import re
import sys
import time

import requests
from requests.auth import HTTPBasicAuth

ROUTER_IP = os.environ.get("ROUTER_IP", "192.168.0.1")
ROUTER_USER = os.environ.get("ROUTER_USER", "admin")
ROUTER_PASS = os.environ.get("ROUTER_PASS", "")
if not ROUTER_PASS:
    print("ERROR: ROUTER_PASS environment variable is required", file=sys.stderr)
    sys.exit(1)
LOG_FILE = "/var/log/router-reboot.log"
AUDIT_LOG_PATH = "/var/log/wcam-login.log"
REBOOT_TIMEOUT = 600   # seconds to wait for full DSL sync + internet
POLL_INTERVAL = 10     # seconds between connectivity checks

# --- Safe logging: try file, fall back to stderr ---
class SafeLogger:
    """Logger that writes to file with stderr fallback on I/O errors."""
    def __init__(self, filename):
        self.filename = filename
        self._broken = False
    def _write_file(self, msg):
        if self._broken:
            return
        try:
            with open(self.filename, 'a') as f:
                f.write(msg + '\n')
        except OSError:
            self._broken = True
    def info(self, msg):
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        line = f'{ts} {msg}'
        self._write_file(line)
        print(line)
    def error(self, msg):
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        line = f'{ts} ERROR: {msg}'
        self._write_file(line)
        print(line, file=sys.stderr)

logger = SafeLogger(LOG_FILE)


def audit_log(event, detail=""):
    """Write an entry to the wcam-login audit log so it appears in the webGUI Event Log."""
    import json
    try:
        entry = {
            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
            "event": event,
            "username": "system",
            "ip": "127.0.0.1",
            "detail": detail
        }
        with open(AUDIT_LOG_PATH, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except OSError:
        pass  # don't break the reboot if audit log is unavailable


def get_session():
    """Create an authenticated session with the router."""
    s = requests.Session()
    s.auth = HTTPBasicAuth(ROUTER_USER, ROUTER_PASS)
    # First request to get a session cookie
    s.get(f"http://{ROUTER_IP}/", timeout=10)
    return s


def get_reboot_form_id(session):
    """Fetch reboot.htm and extract the dynamic form action ID."""
    r = session.get(f"http://{ROUTER_IP}/reboot.htm", timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"reboot.htm returned HTTP {r.status_code}")
    match = re.search(r'action="setup\.cgi\?id=([^"]+)"', r.text)
    if not match:
        raise RuntimeError("Could not find form ID in reboot.htm")
    return match.group(1)


def trigger_reboot(session, form_id):
    """POST the reboot command to setup.cgi."""
    url = f"http://{ROUTER_IP}/setup.cgi?id={form_id}"
    r = session.post(url, data={"todo": "reboot"}, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"Reboot POST returned HTTP {r.status_code}")
    if "reboot_pg.htm" not in r.text:
        raise RuntimeError(f"Unexpected response: {r.text[:200]}")
    return True


def get_router_uptime(session):
    """Fetch router uptime from RST_status.htm.

    The page contains JS variables like: var wan_status = "HH:MM:SS";
    Returns (hours, minutes, seconds) or None if not parseable.
    """
    try:
        r = session.get(f"http://{ROUTER_IP}/RST_status.htm", timeout=5)
        if r.status_code != 200:
            return None
        # Look for: var wan_status = "HH:MM:SS";
        match = re.search(r'var\s+wan_status\s*=\s*["\']([0-9:]+)["\']', r.text)
        if not match:
            return None
        parts = match.group(1).split(":")
        if len(parts) == 3:
            return (int(parts[0]), int(parts[1]), int(parts[2]))
    except Exception:
        pass
    return None


def wait_for_router(timeout=REBOOT_TIMEOUT, interval=POLL_INTERVAL):
    """Wait for the router to fully reboot: web UI + internet + uptime confirmation."""
    start = time.time()

    # Stage 1: wait for router web interface
    logger.info("Stage 1: Waiting for router web interface...")
    web_timeout = 120  # 2 minutes for router OS to boot
    web_session = None
    while time.time() - start < web_timeout:
        try:
            r = requests.get(f"http://{ROUTER_IP}/", timeout=5)
            if r.status_code == 401:
                elapsed = int(time.time() - start)
                logger.info("Router web interface is back online (HTTP 401) — %ds", elapsed)
                # Re-authenticate to read status pages
                web_session = get_session()
                break
        except (requests.ConnectionError, requests.Timeout):
            pass
        elapsed = int(time.time() - start)
        logger.info(f"  ... still waiting for web UI ({elapsed}s / {web_timeout}s)")
        time.sleep(interval)
    else:
        raise RuntimeError(f"Router web interface did not come back within {web_timeout}s")

    # Stage 2: wait for actual internet connectivity (DSL sync + PPP)
    logger.info("Stage 2: Waiting for internet connectivity (DSL sync)...")
    while time.time() - start < timeout:
        try:
            r = requests.get("https://1.1.1.1", timeout=5)
            if r.status_code == 200:
                elapsed = int(time.time() - start)
                logger.info("Internet connectivity confirmed (1.1.1.1) — %ds", elapsed)
                break
        except (requests.ConnectionError, requests.Timeout):
            pass
        elapsed = int(time.time() - start)
        logger.info(f"  ... still waiting for internet ({elapsed}s / {timeout}s)")
        time.sleep(interval)
    else:
        raise RuntimeError(f"Internet did not come back within {timeout}s (web UI was up but DSL did not sync)")

    # Stage 3: confirm reboot via router uptime (< 5 minutes = fresh reboot)
    logger.info("Stage 3: Checking router uptime to confirm reboot...")
    if web_session:
        uptime = get_router_uptime(web_session)
        if uptime:
            h, m, s = uptime
            total_minutes = h * 60 + m
            uptime_str = "%dh %dm %ds" % (h, m, s)
            logger.info("Router uptime: %s", uptime_str)
            if total_minutes >= 5:
                raise RuntimeError(
                    "Router uptime is %s — does not look like a fresh reboot" % uptime_str
                )
            logger.info("Uptime confirms recent reboot (< 5 min)")
            return uptime_str
        else:
            logger.info("Could not read router uptime (status page may have changed)")
            return None
    return None


def main():
    parser = argparse.ArgumentParser(description="Reboot the Netgear D7000 router")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch reboot page and form ID but do not POST")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Router reboot script started")
    if args.dry_run:
        logger.info("DRY RUN mode — will not actually reboot")

    start_time = time.time()
    try:
        session = get_session()
        logger.info("Authenticated with router")

        form_id = get_reboot_form_id(session)
        logger.info(f"Got reboot form ID: {form_id}")

        if args.dry_run:
            logger.info("DRY RUN complete — form ID retrieved, no reboot sent")
            return 0

        logger.info("Sending reboot command...")
        trigger_reboot(session, form_id)
        logger.info("Reboot command sent successfully")
        audit_log("ROUTER_REBOOT_SENT", "Reboot command sent to router at %s" % ROUTER_IP)

        uptime_str = wait_for_router()
        elapsed = int(time.time() - start_time)
        mins, secs = divmod(elapsed, 60)
        detail = "Router at %s rebooted successfully in %d min %d sec" % (ROUTER_IP, mins, secs)
        if uptime_str:
            detail += " (uptime: %s)" % uptime_str
        logger.info("Router reboot complete and verified (%d min %d sec)" % (mins, secs))
        audit_log("ROUTER_REBOOT_OK", detail)
        return 0

    except Exception as e:
        logger.error(f"Router reboot FAILED: {e}")
        audit_log("ROUTER_REBOOT_FAIL", str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
