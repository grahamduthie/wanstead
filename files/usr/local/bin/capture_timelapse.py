#!/usr/bin/env python3
"""
Capture a frame from the camera for timelapse.
Goes to Preset 1 position, waits for settle, captures JPEG.
Only captures during daylight hours (30 min before sunrise to 30 min after sunset).
"""

import subprocess
import os
import sys
from datetime import datetime, timedelta
from astral import LocationInfo
from astral.sun import sun

NAS_MOUNT = "/mnt/nas"
TIMELAPSE_DIR = os.path.join(NAS_MOUNT, "timelapse")
PRESET_BIN = "/usr/local/bin/ptz-preset"
USTREAMER_URL = "http://localhost:8080/snapshot"
CAMERA_LAT = 51.48
CAMERA_LON = -1.0
BUFFER_MINUTES = 30
CAPTURE_INTERVAL = 5


def get_daylight_window(date):
    """Get capture window for a given date (in local timezone)."""
    import pytz

    tz = pytz.timezone("Europe/London")
    city = LocationInfo("Twyford", "Berkshire", "Europe/London", CAMERA_LAT, CAMERA_LON)
    s = sun(city.observer, date=date)
    start = tz.normalize(
        s["sunrise"].astimezone(tz) - timedelta(minutes=BUFFER_MINUTES)
    )
    end = tz.normalize(s["sunset"].astimezone(tz) + timedelta(minutes=BUFFER_MINUTES))
    return start, end


def is_daylight():
    """Check if current time is within daylight capture window."""
    import pytz

    tz = pytz.timezone("Europe/London")
    now = datetime.now(tz)
    start, end = get_daylight_window(now.date())
    return start <= now <= end


def ensure_dir(path):
    """Create directory if it doesn't exist."""
    if not os.path.exists(path):
        os.makedirs(path)


def go_to_preset1():
    """Move camera to Preset 1 position."""
    try:
        result = subprocess.run([PRESET_BIN, "home"], capture_output=True, timeout=5)
        if result.returncode != 0:
            print(f"Home failed: {result.stderr.decode()}")
            return False
    except Exception as e:
        print(f"Home error: {e}")
        return False

    import time

    time.sleep(1.5)

    try:
        result = subprocess.run([PRESET_BIN, "preset1"], capture_output=True, timeout=5)
        if result.returncode != 0:
            print(f"Preset 1 failed: {result.stderr.decode()}")
            return False
    except Exception as e:
        print(f"Preset 1 error: {e}")
        return False

    time.sleep(1.5)
    return True


def capture_frame():
    """Capture a single frame from ustreamer."""
    import urllib.request
    import hashlib

    today_dir = os.path.join(TIMELAPSE_DIR, datetime.now().strftime("%Y-%m-%d"))
    ensure_dir(today_dir)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{timestamp}.jpg"
    filepath = os.path.join(today_dir, filename)

    try:
        req = urllib.request.Request(
            USTREAMER_URL, headers={"User-Agent": "WagtailCam Timelapse"}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = response.read()

        with open(filepath, "wb") as f:
            f.write(data)

        size = os.path.getsize(filepath)
        return filepath, size
    except Exception as e:
        print(f"Capture error: {e}")
        return None, None


def main():
    if not os.path.ismount(NAS_MOUNT):
        print(f"NAS not mounted at {NAS_MOUNT}")
        sys.exit(1)

    if not is_daylight():
        start, end = get_daylight_window(datetime.now().date())
        print(
            f"Outside daylight hours. Window: {start.strftime('%H:%M')} - {end.strftime('%H:%M')}"
        )
        sys.exit(0)

    print(f"Moving to Preset 1...")
    if not go_to_preset1():
        sys.exit(1)

    print(f"Capturing frame...")
    filepath, size = capture_frame()
    if filepath:
        print(f"Captured: {filepath} ({size} bytes)")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
