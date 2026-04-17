#!/usr/bin/env python3
"""
Capture a frame from the camera for timelapse.
Goes to Preset 1 position, applies saved focus, waits for settle, captures JPEG.
Only captures during daylight hours (30 min before sunrise to 30 min after sunset).
"""

import subprocess
import os
import sys
import json
from datetime import datetime, timedelta
from astral import LocationInfo
from astral.sun import sun

NAS_MOUNT = "/mnt/nas"
TIMELAPSE_DIR = os.path.join(NAS_MOUNT, "timelapse")
PRESET_BIN = "/usr/local/bin/ptz-preset"
USTREAMER_URL = "http://localhost:8080/snapshot"
DEVICE = "/dev/video0"
CAMERA_LAT = 51.48
CAMERA_LON = -1.0
BUFFER_MINUTES = 30
CAPTURE_STATUS_FILE = "/var/www/camviewer/.capture_status.json"
PRESET_NAMES_FILE = "/var/www/camviewer/.preset_names.json"


def set_capture_status(capturing, message=""):
    """Set capture status for browser notification."""
    import json

    try:
        with open(CAPTURE_STATUS_FILE, "w") as f:
            json.dump({"capturing": capturing, "message": message}, f)
    except Exception as e:
        print(f"Status write error: {e}")


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


def load_preset_focus(preset_num):
    """Load focus value for a preset. Returns None if not set."""
    try:
        with open(PRESET_NAMES_FILE, "r") as f:
            data = json.load(f)
            key = str(preset_num)
            if key in data and isinstance(data[key], dict):
                return data[key].get("focus")
            return None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def set_camera_focus(focus_value):
    """Set camera focus to a specific value (0-255) and disable auto-focus."""
    try:
        subprocess.run(
            ["v4l2-ctl", "-d", DEVICE, "-c", f"focus_automatic_continuous=0"],
            capture_output=True,
            timeout=2,
        )
        subprocess.run(
            ["v4l2-ctl", "-d", DEVICE, "-c", f"focus_absolute={focus_value}"],
            capture_output=True,
            timeout=2,
        )
        return True
    except Exception as e:
        print(f"Focus set error: {e}")
        return False


def go_to_preset1():
    """Move camera to Preset 1 position and apply saved focus."""
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

    focus = load_preset_focus(1)
    if focus is not None:
        print(f"Applying saved focus: {focus}")
        set_camera_focus(focus)
        time.sleep(2.0)

    return True


def capture_frame():
    """Capture a single frame from ustreamer with JPEG validation."""
    import urllib.request

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

        if len(data) < 1000:
            print(f"Capture error: response too small ({len(data)} bytes)")
            return None, None

        if data[0:2] != b"\xff\xd8":
            print(f"Capture error: not a valid JPEG (magic: {data[0:3].hex()})")
            return None, None

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
    set_capture_status(True, "Timelapse capture...")

    if not go_to_preset1():
        set_capture_status(False)
        sys.exit(1)

    print(f"Capturing frame...")
    filepath, size = capture_frame()

    set_capture_status(False)

    if filepath:
        print(f"Captured: {filepath} ({size} bytes)")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
