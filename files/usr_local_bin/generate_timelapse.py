#!/usr/bin/env python3
"""Generate a timelapse MP4 for a given date from JPEG frames on the NAS.

Usage:
    generate_timelapse.py                # yesterday (default)
    generate_timelapse.py 2026-04-18     # specific date
    generate_timelapse.py --all          # all past dates that don't yet have a video
"""

import os
import shutil
import sys
import subprocess
import tempfile
from datetime import date, timedelta

TIMELAPSE_DIR = "/mnt/nas/timelapse"


def generate(date_str):
    date_dir = os.path.join(TIMELAPSE_DIR, date_str)
    if not os.path.isdir(date_dir):
        print(f"{date_str}: directory not found, skipping")
        return False

    jpg_files = sorted(f for f in os.listdir(date_dir) if f.endswith(".jpg"))
    if not jpg_files:
        print(f"{date_str}: no images, skipping")
        return False

    video_path = os.path.join(date_dir, "timelapse.mp4")

    print(f"{date_str}: generating video from {len(jpg_files)} frames...", flush=True)

    fd_list, filelist = tempfile.mkstemp(suffix=".txt")
    # Write to local /tmp — writing directly to the NAS over CIFS is unreliable
    # for large ffmpeg output. We copy to the NAS once encoding succeeds.
    fd_out, tmp_out = tempfile.mkstemp(suffix=".tmp.mp4")
    os.close(fd_out)
    try:
        with os.fdopen(fd_list, "w") as f:
            for jpg in jpg_files:
                f.write(f"file '{os.path.join(date_dir, jpg)}'\n")
                f.write("duration 0.1\n")
            # Repeat last file without duration — ffmpeg concat quirk to seal
            # the final frame's duration and ensure it appears in the output.
            f.write(f"file '{os.path.join(date_dir, jpg_files[-1])}'\n")

        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0", "-i", filelist,
                "-r", "10",
                "-vf", "scale=1280:720",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                tmp_out,
            ],
            capture_output=True,
            timeout=600,
        )

        if result.returncode == 0:
            shutil.copy2(tmp_out, video_path)
            size_mb = os.path.getsize(video_path) / (1024 * 1024)
            print(f"{date_str}: done — {size_mb:.1f} MB")
            return True
        else:
            stderr_tail = result.stderr.decode(errors="replace")[-300:]
            print(f"{date_str}: ffmpeg failed:\n{stderr_tail}")
            return False

    except Exception as e:
        print(f"{date_str}: error — {e}")
        return False

    finally:
        for p in (filelist, tmp_out):
            try:
                os.unlink(p)
            except OSError:
                pass


def main():
    args = sys.argv[1:]

    if "--all" in args:
        if not os.path.isdir(TIMELAPSE_DIR):
            print(f"Timelapse directory {TIMELAPSE_DIR} not found")
            sys.exit(1)
        today = date.today().isoformat()
        dates = sorted(
            d for d in os.listdir(TIMELAPSE_DIR)
            if os.path.isdir(os.path.join(TIMELAPSE_DIR, d)) and d < today
        )
        if not dates:
            print("No past dates found")
            return
        for ds in dates:
            video_path = os.path.join(TIMELAPSE_DIR, ds, "timelapse.mp4")
            if os.path.isfile(video_path):
                print(f"{ds}: video already exists, skipping")
            else:
                generate(ds)

    elif args and not args[0].startswith("-"):
        generate(args[0])

    else:
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        generate(yesterday)


if __name__ == "__main__":
    main()
