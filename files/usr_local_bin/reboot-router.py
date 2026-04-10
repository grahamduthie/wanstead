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
REBOOT_TIMEOUT = 180   # seconds to wait for router to come back
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


def wait_for_router(timeout=REBOOT_TIMEOUT, interval=POLL_INTERVAL):
    """Wait for the router to come back online after reboot."""
    logger.info("Waiting for router to reboot...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"http://{ROUTER_IP}/", timeout=5)
            if r.status_code == 401:
                # 401 means the web interface is up (auth required) — router is back
                logger.info("Router web interface is back online (HTTP 401)")
                return True
        except (requests.ConnectionError, requests.Timeout):
            pass
        elapsed = int(time.time() - start)
        logger.info(f"  ... still waiting ({elapsed}s / {timeout}s)")
        time.sleep(interval)
    raise RuntimeError(f"Router did not come back online within {timeout}s")


def main():
    parser = argparse.ArgumentParser(description="Reboot the Netgear D7000 router")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch reboot page and form ID but do not POST")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Router reboot script started")
    if args.dry_run:
        logger.info("DRY RUN mode — will not actually reboot")

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

        wait_for_router()
        logger.info("Router reboot complete and verified")
        return 0

    except Exception as e:
        logger.error(f"Router reboot FAILED: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
