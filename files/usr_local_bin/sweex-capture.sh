#!/bin/bash
# Periodic still capturer for Sweex Mini Webcam (/dev/video2)
# Uses v4l2-ctl -w (libv4l2 wrapper) to decode S910/BA81 -> RGB,
# captures 5 frames (first few are autogain warmup), uses the last one.
# Applies gentle contrast/gamma/saturation boost to fix the narrow
# dynamic range from the gspca/sonixb driver's S910 decoder.
#
# Writes to tmpfs (/var/www/camviewer/sweex-tmpfs/) to avoid SD card wear.
# A symlink at /var/www/camviewer/sweex_latest.jpg points to the tmpfs file.

OUT=/var/www/camviewer/sweex-tmpfs/sweex_latest.jpg
INTERVAL=5
FRAME_W=176
FRAME_H=144
FRAME_BYTES=$((FRAME_W * FRAME_H * 3))
TOTAL_FRAMES=5

while true; do
    # Use unique temp filenames per iteration to avoid permission issues
    TMP_RAW=$(mktemp /tmp/sweex_raw_XXXXXX.raw)
    TMP_FRAME=$(mktemp /tmp/sweex_frame_XXXXXX.raw)
    TMP_JPG=$(mktemp /tmp/sweex_new_XXXXXX.jpg)

    if v4l2-ctl -w -d /dev/video2 \
        --set-fmt-video=width=${FRAME_W},height=${FRAME_H},pixelformat=S910 \
        --stream-mmap --stream-count=${TOTAL_FRAMES} \
        --stream-to="$TMP_RAW" >/dev/null 2>&1; then

        # Extract last frame (skip warmup frames)
        python3 -c "
import sys
data = open('$TMP_RAW','rb').read()
frame = data[-${FRAME_BYTES}:]
if len(frame) == ${FRAME_BYTES}:
    open('$TMP_FRAME','wb').write(frame)
    sys.exit(0)
sys.exit(1)
" && \
        ffmpeg -y \
            -f rawvideo -pixel_format rgb24 \
            -video_size ${FRAME_W}x${FRAME_H} \
            -i "$TMP_FRAME" \
            -vf "eq=contrast=1.3:saturation=1.15:gamma=1.05,scale=352:288" \
            -update 1 -frames:v 1 \
            "$TMP_JPG" >/dev/null 2>&1 && \
        mv "$TMP_JPG" "$OUT" && chmod 644 "$OUT"
    fi

    # Clean up temp files
    rm -f "$TMP_RAW" "$TMP_FRAME" "$TMP_JPG"

    sleep "$INTERVAL"
done
