# Wanstead Pi — Webcam Project

**Date:** 7 April 2026
**Updated:** 13 April 2026 (12:30 BST) — Unified WebSocket relay for all browsers, removed native MJPEG path (Chrome/Firefox/Edge), removed 5s watchdog reload
**Device:** Raspberry Pi (cellpi, kernel 6.12.75+rpt-rpi-v8, aarch64)
**IP:** 192.168.0.18
**Public IP:** 90.251.55.4 (dynamic, BT)
**Domain:** https://wansteadcam.lumoco.com (Namecheap Dynamic DNS, Let's Encrypt TLS)
**Router:** Netgear D7000 (192.168.0.1) — port forwards 80, 443 → 192.168.0.18

**Note:** This document is maintained on the Pi itself (`/home/gduthie/wanstead/QWEN.md`).

---

## Goal

Set up remote webcam viewing via a web application on the Pi, accessible from any browser on the local network and the internet.

---

## Hardware

### Cameras Tested

| Camera | USB ID | Driver | Status |
|--------|--------|--------|--------|
| Unknown (first attempt) | — | — | ❌ Not detected — USB enumeration failure |
| **Logitech QuickCam Pro 5000** | `046d:08c5` | `uvcvideo` (UVC 1.00) | ✅ **Fully working** |
| **Sweex Mini Webcam** | `0c45:6005` | `gspca_main` / `sonixb` | ✅ **Working** — periodic stills via libv4l2 |

### USB Topology

```
Pi root hub (dwc_otg)
└── SMSC USB 2.0 Hub (0424:2514) — built-in Pi hub
    └── SMSC USB 2.0 Hub (0424:2514) — second tier
        ├── SMSC LAN7800 (0424:7800) — Pi's onboard Ethernet (lan78xx)
        ├── Logitech QuickCam Pro 5000 (046d:08c5) → /dev/video0
        └── Sweex Mini Webcam (0c45:6005) → /dev/video2
```

**Note:** The LTE dongle (`05c6:90b4`) is no longer connected.

---

## What Works

### Logitech QuickCam Pro 5000 (`/dev/video0`)

- **Native UVC** — plug-and-play, no extra drivers needed
- **Resolutions:** 160x120 up to 640x480
- **Formats:** MJPEG and YUYV 4:2:2
- **Frame rates:** Up to 30fps at all resolutions
- **Streaming:** ustreamer on port 8080 — works perfectly
- **Still images:** `fswebcam` works fine

### Sweex Mini Webcam (`/dev/video2`)

- **Periodic stills** every 5 seconds, served as a static JPEG refreshed by JS
- **Working capture method:** `v4l2-ctl -w` (libv4l2 wrapper, `-w` flag) requesting RGB3 format, capturing 5 frames and discarding the first 4 (autogain warmup), then converting the last frame to JPEG via ffmpeg
- **Resolution:** 176x144 native, upscaled to 352x288 in output
- **Service:** `ustreamer-sweex.service` runs `/usr/local/bin/sweex-capture.sh`
- **Image processing:** The gspca/sonixb driver's S910→RGB software decoder produces images with a narrow dynamic range (~85–186 instead of 0–255), resulting in flat, washed-out output. This is inherent to how the driver decodes the proprietary SN9C10X compressed format.
- **Current filter:** `eq=contrast=1.3:saturation=1.15:gamma=1.05` — stretches the narrow range, lifts midtones gently via gamma > 1.0, and adds modest color saturation. Resulting pixel range: ~84–220 (vs original 85–186), preserving detail while improving contrast and brightness.

#### Sweex Image Quality Evolution (8 April 2026)

| Step | Filter | Range | Result |
|------|--------|-------|--------|
| Baseline | None | 85–186 | Washed out, narrow dynamic range |
| Attempt 1 | `histeq=strength=0.8` | 0–255 | Too aggressive — crushed midtone detail, excessive contrast |
| Attempt 2 | `eq=contrast=1.3:gamma=0.9:sat=1.15` | 69–204 | Good contrast balance, but slightly dark |
| Attempt 3 | `eq=contrast=1.3:brightness=10:…` | clipped to 255 | Way too bright — blank white image |
| Attempt 4 | `eq=contrast=1.3:brightness=1:…` | 93–255 | Still clipping — ffmpeg's `brightness` param is too coarse |
| **Current** | `eq=contrast=1.3:sat=1.15:gamma=1.05` | 84–220 | ✅ Good lift in midtones without clipping highlights or crushing shadows |

**Key lesson:** ffmpeg's `brightness` parameter is extremely sensitive — even `brightness=1` caused severe clipping. Using `gamma` > 1.0 to lift midtones is a much gentler and more controllable approach.

#### Sweex Pixel Format Fix (8 April 2026, 19:06 BST)

After a Pi reboot, the Sweex camera stopped working. Investigation revealed:
- **Symptom:** `v4l2-ctl -w` reported `The pixelformat 'RGB3' is invalid`
- **Root cause:** The script was requesting `pixelformat=RGB3`, which is **not** a native format the driver provides. The `-w` flag (libv4l2 wrapper) converts S910→RGB internally, but the kernel driver itself only accepts `S910` or `BA81`.
- **Why it worked before:** Unclear — possibly the camera device retained a working state, or libv4l2 was more lenient in earlier sessions.
- **Fix:** Changed `pixelformat=RGB3` to `pixelformat=S910` in `/usr/local/bin/sweex-capture.sh`. The `-w` flag still performs the S910→RGB conversion via libv4lconvert.
- **Additional note:** After reboot, the camera's default resolution reverted to `352x288` (from `176x144`). The script explicitly sets `176x144` which the driver accepts.

**Correct format chain:** `S910 (driver)` → `libv4lconvert_decode_sn9c10x()` → `RGB24` → `ffmpeg eq filter` → `JPEG`

---

## What Doesn't Work / Key Lessons

### Why the driver appeared broken (and how we fixed it)

The `gspca/sonixb` driver on kernel 6.12 supports only two formats:
- `S910` — proprietary SN9C10X compressed format
- `BA81` — raw 8-bit Bayer BGBG/GRGR

**The driver has NOT regressed on kernel 6.12.** The last substantive driver changes were in 2013. The apparent breakage was caused by applications requesting MJPEG or YUYV, formats the driver cannot provide. The kernel silently falls back to BA81, which applications then misinterpret as YUV/luminance data, producing near-black or corrupted output.

**The fix:** use `libv4l2` via the `-w` flag in `v4l2-ctl`. This routes S910/BA81 through libv4lconvert's software decoder (`v4lconvert_decode_sn9c10x()` in libv4lconvert/sn9c10x.c), which produces correct RGB output.

**The warmup issue:** The first few frames after opening the device are near-black due to the autogain settling. Capturing 5 frames and using only the last one resolves this.

### Approaches that do NOT work

| Approach | Result |
|----------|--------|
| `ustreamer` with S910/BA81 format | ❌ Unsupported or wrong format |
| `ffmpeg -f v4l2 -input_format yuyv422` | ❌ Receives BA81, misinterprets as YUV → near-black |
| `v4l2-ctl` raw BA81 capture + ffmpeg bayer debayer | ❌ Autogain frames = constant-value pixels |
| `fswebcam` output directly | ❌ Produces near-black JPEG (same misinterpretation issue) |
| Direct MJPEG streaming | ❌ Not feasible — format not supported by driver |
| `sn9c102` legacy driver | ❌ Removed from kernel at 3.17, no modern fork |

### First Camera — Not Detected

USB enumeration failure — bad cable, insufficient power, or faulty device.

---

## Current Architecture

```
Internet → Port 80  (Netgear forward) → nginx:80  → 301 redirect → HTTPS:443
Internet → Port 443 (Netgear forward) → nginx:443 → TLS + session cookie auth → webcam viewer
LAN      → Port 8085                  → nginx:8085 → same content, no auth
LAN      → Port 8080                  → ustreamer:8080 → /dev/video0 (Logitech, live MJPEG)
Internal → Port 8086 (127.0.0.1 only) → wcam-auth  → Flask auth backend (login, verify, logout)
Internal → 192.168.0.1 (HTTP)          → reboot-router.py → weekly router reboot (Mon 04:00)

wansteadcam.lumoco.com ──DDNS──→ 90.251.55.4 ──port 443──→ 192.168.0.18:443 (TLS)
                                                          ──port 80───→ 192.168.0.18:80  (→ 301 → 443)
```

### Auth Flow

1. Unauthenticated user visits `https://wansteadcam.lumoco.com/` → nginx `auth_request` calls `127.0.0.1:8086/api/verify` → no valid cookie → nginx serves `/login.html`
2. User enters credentials → POST `/api/login` → Flask validates against bcrypt htpasswd → sets `wcam_session` cookie (24h TTL, httponly)
3. Subsequent requests include cookie → nginx `auth_request` verifies → dashboard and streams served
4. No Basic Auth browser dialog — clean login page with logo

### Services

| Service | Port | Status | Notes |
|---------|------|--------|-------|
| **nginx** | 80, 443, 8085 | ✅ Running | 80 = HTTP→HTTPS redirect; 443 = TLS + session cookie auth + rate limiting; 8085 = LAN-only, no auth |
| **wcam-auth** | 8086 (127.0.0.1 only) | ✅ Running | Waitress WSGI server (production-grade) — login, verify, logout. Not internet-facing. |
| **wcam-ws-relay** | 8087 (127.0.0.1 only) | ✅ Running | WebSocket MJPEG relay for Safari/iOS — 30fps via canvas. Not internet-facing. |
| **ustreamer-logitech** | 8080 | ✅ Running | 640x480, 30fps, MJPEG — LAN only. `--slowdown` flag: drops to 1 FPS when no clients connected (30x CPU reduction) |
| **ustreamer-sweex** | — | ✅ Running | Periodic still capturer, no port needed |
| **fail2ban** | — | ✅ Running | SSH jail (3 fails → 1h ban), wcam-auth jail (5 fails → 1h ban) |
| **camviewer** | — | 🚫 Masked | Conflicting Flask server — removed and masked to prevent port 8085 conflict |

### SSL / TLS

| Item | Value |
|------|-------|
| Provider | Let's Encrypt (via certbot) |
| Certificate | `/etc/letsencrypt/live/wansteadcam.lumoco.com/fullchain.pem` |
| Private key | `/etc/letsencrypt/live/wansteadcam.lumoco.com/privkey.pem` |
| Key type | ECDSA |
| Valid until | 7 July 2026 |
| Renewal method | certbot `webroot` via systemd timer (twice daily) |
| Deploy hook | `/etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh` — reloads nginx after renewal |
| HSTS | `max-age=31536000; includeSubDomains` — browsers enforce HTTPS for returning visitors |

HTTP on port 80 returns a `301` redirect to `https://`. All internet-facing traffic goes through TLS.

### Authentication

Session-based login on port 443 with role-based user management (9 April 2026):

| User | Password | Role |
|------|----------|------|
| lmorton | _set locally_ | Admin |
| gduthie | _set locally_ | Admin |

**How it works:**
- Unauthenticated users see a login page (`/login.html`) with the WansteadCam logo
- Credentials validated against `/etc/nginx/.wcam-users.json` (bcrypt `$2y$` hashes) via Waitress WSGI server on `127.0.0.1:8086`
- On success, a secure `wcam_session` cookie is set (httponly, samesite=Lax, **secure=True**, 24h TTL) — includes `is_admin` flag
- nginx `auth_request` checks the cookie on every protected request; passes `X-Auth-Admin` header (1 or 0)
- Invalid/missing cookie → login page; valid cookie → dashboard + streams
- `/api/logout` clears the session cookie and logs the event
- Login page uses `window.location.replace('/')` to force a fresh navigation (not cached)
- nginx serves `/login.html` with `Cache-Control: no-cache, no-store, must-revalidate` to prevent stale login page after auth

**User management (admin only):**
- Admin users see a **⚙ Admin** button next to Logout in the header
- Opens a modal with two tabs:
  - **Users tab**: user table (username, role badge, Edit/Delete actions), Add User form
  - **Login Log tab**: paginated audit log viewer with event type and username filters, color-coded events, full date+time shown (MM-DD HH:MM)
- Edit modal allows changing username, password (leave blank to keep current), and admin flag
- Cannot delete the last admin user (safety guard)
- All user changes are logged to the audit log with the acting admin's username

**API endpoints:**

| Endpoint | Method | Access | Purpose |
|----------|--------|--------|---------|
| `/api/login` | POST | Public | Authenticate, set session cookie |
| `/api/verify` | GET | Public (nginx) | Validate session for `auth_request` |
| `/api/me` | GET | Authenticated | Return `{username, is_admin}` for frontend |
| `/api/logout` | POST | Authenticated | Destroy session, log event |
| `/api/users` | GET | Admin only | List all users (no hashes returned) |
| `/api/users` | POST | Admin only | Create new user |
| `/api/users/<name>` | PUT | Admin only | Update username, password, or admin flag |
| `/api/users/<name>` | DELETE | Admin only | Delete user (blocked if last admin) |
| `/api/audit` | GET | Admin only | Paginated login audit log (filters: event, username) |

**Login audit log:**
- File: `/var/log/wcam-login.log`
- Format: JSON lines — `{timestamp, event, username, ip, detail}`
- Events: `LOGIN_OK`, `LOGIN_FAIL`, `LOGOUT`, `USER_CREATED`, `USER_UPDATED`, `USER_DELETED`
- Rotation: Python `TimedRotatingFileHandler` — daily, 365 backups (1 year retention)
- Rotated files named with date suffix: `wcam-login.log.2026-04-09`

**Why not Basic Auth:** Browser-native Basic Auth dialogs are a magnet for automated scanning and brute-force attacks. A custom login page is less conspicuous and gives us control over the UX.

**User database:** `/etc/nginx/.wcam-users.json` — JSON file with `{username: {hash, is_admin}}`. Atomic writes via temp file + `os.replace()`. File locking with `fcntl` for thread safety under Waitress's 4-thread pool.

### Dynamic DNS

| Item | Value |
|------|-------|
| Provider | Namecheap (built-in DDNS) |
| Domain | `wansteadcam.lumoco.com` |
| Script | `/usr/local/bin/namecheap-ddns.sh` |
| Schedule | Cron, every 5 minutes |
| Log | `/var/log/namecheap-ddns.log` |
| State file | `/var/run/namecheap-ddns-ip` |

The script checks the current public IP via `api.ipify.org`, compares it to the last known value, and calls Namecheap's DDNS API only if it has changed.

### Router Auto-Reboot

The Netgear D7000 has a firmware bug that causes the DSL connection to drop after ~600 hours of uptime. A weekly reboot prevents this.

| Item | Value |
|------|-------|
| Router | Netgear D7000 (192.168.0.1), firmware V1.0.1.84_1.0.1 |
| Admin credentials | admin / password |
| Reboot method | HTTP POST to `setup.cgi?id=<dynamic_id>` with `todo=reboot` |
| Script | `/usr/local/bin/reboot-router.py` |
| Schedule | Cron, every Monday at 04:00 BST |
| Log | `/var/log/router-reboot.log` |

**How it works:**
1. Authenticate to router web interface via HTTP Basic Auth
2. Fetch `reboot.htm` to get a dynamic form action ID (changes each session)
3. POST to `setup.cgi?id=<id>` with `todo=reboot`
4. Wait up to 180 seconds for the router to come back online (polls for HTTP 401)
5. Log success or failure

**Confirmed working:** Tested live on 9 April 2026. Router rebooted at 09:44, DSL up at 09:45, internet restored at 09:45.

**Manual commands:**
```bash
sudo /usr/local/bin/reboot-router.py          # Live reboot
sudo /usr/local/bin/reboot-router.py --dry-run  # Test without rebooting
cat /var/log/router-reboot.log                 # View reboot history
```

### Unattended Operation (9 April 2026 — Hardened)

The Pi is designed to run unattended for long periods (possibly years). The following measures ensure reliability:

#### Log Rotation

| Log | Rotation | Retention | Max Size |
|-----|----------|-----------|----------|
| `/var/log/wcam-auth.log` | App-level: RotatingFileHandler + logrotate safety net | 5 × 1MB backups | ~6MB |
| `/var/log/wcam-login.log` | App-level: TimedRotatingFileHandler (daily) | **365 days (1 year)** | ~5MB |
| `/var/log/wcam-ws-relay.log` | App-level: RotatingFileHandler | 3 × 500KB backups | ~2MB |
| `/var/log/nginx/*.log` | Daily (logrotate) | 14 days, compressed | ~14MB |
| `/var/log/fail2ban.log` | Weekly (logrotate) | 4 weeks, compressed | ~4MB |
| `/var/log/letsencrypt/*.log` | Weekly (logrotate) | 12 weeks, compressed | ~12MB |
| `/var/log/router-reboot.log` | Monthly (logrotate) | 12 months, compressed | ~1MB |
| `/var/log/namecheap-ddns.log` | Monthly (logrotate) | 12 months, compressed | ~1MB |
| systemd journal | journald.conf limits | 1 month max | 50MB persistent, 20MB runtime |

#### SD Card Wear Reduction

| Measure | Impact |
|---------|--------|
| `noatime` mount on root filesystem | Eliminates read-time metadata writes |
| `/tmp` as tmpfs (464MB) | All temp files in RAM |
| Sweex image on tmpfs (`/var/www/camviewer/sweex-tmpfs`, 2MB) | **Eliminates ~17,280 writes/day** from Sweex camera (written every 5s). Symlink at `sweex_latest.jpg` → tmpfs. |
| Weekly `/tmp` cleanup cron (Sun 03:30) | Removes stale files >7 days old |

#### CPU Idle Mode

| Service | Active (with clients) | Idle (no clients) | Mechanism |
|---------|----------------------|-------------------|-----------|
| ustreamer-logitech | ~1-2% (30fps MJPEG) | ~0.1% (1 FPS) | `--slowdown` flag — drops to 1 FPS when no stream clients connected |
| wcam-ws-relay | ~0.5% (broadcasting to clients) | ~0.1% (connected to ustreamer, no WS clients) | asyncio event loop — reads stream but only broadcasts when clients connected |
| sweex-capture | ~0.1% (sleeping) | ~0.1% (unchanged) | Already minimal — sleeps 5s between captures |

#### Stream Auto-Reconnect (9 April 2026, updated 13 April)

All browsers use the same WebSocket relay — reconnect behavior is consistent across all platforms.

| Scenario | Trigger | Recovery |
|----------|---------|----------|
| **Any browser, tab backgrounded** | `visibilitychange` fires | WS connection closed; reconnect on tab visible |
| **Any browser, connection dropped** | `ws.onclose` fires | Reconnect with exponential backoff (1s → 2s → 4s → ... → 10s max) |
| **Any browser, tab becomes visible** | `visibilitychange` event | Reconnect + reset backoff timer |

**Implementation:** JavaScript in `/var/www/camviewer/index.html` — WebSocket relay IIFE with:
- `ws.onclose` → schedules reconnect with exponential backoff
- `visibilitychange` listener → closes WS when hidden, reconnects when visible
- `isDecoding` guard + `latestFrame` buffer + `rAF` sync → smooth playback

**Before this fix:** User had to manually refresh the browser to restore the stream after backgrounding the tab or waking the device.

#### Stream via WebSocket Relay (9 April 2026 — Smooth Playback for All Browsers)

All browsers use the same WebSocket-based MJPEG relay for smooth, consistent playback.

**How it works:** WebSocket connection to `/ws/logitech/`. A Python asyncio relay server (`ws_relay.py`) reads the MJPEG stream from ustreamer, extracts individual JPEG frames, and pushes them as binary WebSocket messages. Each frame is drawn onto a `<canvas>` element. Throttled to ~20fps for smooth playback. Polling pauses when the tab is hidden (`visibilitychange`) and resumes when visible.

**Before this fix:** Chrome/Firefox/Edge used native MJPEG via `<img>` which was jumpy — browser throttling, GC pauses, and connection drops caused visible stutter. Safari/iOS didn't handle native MJPEG at all. Native MJPEG also suffered from silent connection drops when tabs were backgrounded, with no automatic recovery.

**13 April 2026 — Unified WebSocket for all browsers:** Removed the native MJPEG `<img>` fallback path for Chrome/Firefox/Edge. All browsers now use the WebSocket relay exclusively. This eliminates browser-specific behavior, silent connection drops, and the need for watchdog timers. The `isSafari`/`isIOS` browser detection variables were removed.

**Why not native MJPEG:** The browser's async JPEG decoder fires decode requests that can pile up and reorder, causing frames to appear out of sequence or stutter. The WebSocket relay decouples frame receive from render, preventing decode pileup entirely. Additionally, browsers aggressively throttle background tab MJPEG connections, causing streams to go blank with no automatic recovery.

**WebSocket relay architecture:**

| Component | Detail |
|-----------|--------|
| **Server** | `/var/www/camviewer/ws_relay.py` — asyncio + `websockets` library |
| **Listen** | `127.0.0.1:8087` (localhost only, not internet-facing) |
| **Upstream** | Connects to `127.0.0.1:8080/stream` (ustreamer MJPEG) |
| **Protocol** | Parses `--boundarydonotcross` multipart boundary, extracts JPEG frames (FF D9 marker), broadcasts to all connected WS clients |
| **Throttle** | 50ms minimum between frames (~20fps max) — prevents Safari decode queue buildup and GC-induced stutter |
| **Frontend decode guard** | `isDecoding` flag prevents concurrent JPEG decodes; `latestFrame` buffer always holds newest frame; `requestAnimationFrame` gate syncs canvas draws to display refresh |
| **Service** | `wcam-ws-relay.service` — `Restart=always`, enabled |
| **nginx proxy** | `/ws/logitech/` → `127.0.0.1:8087` with `Upgrade`/`Connection` headers, 1h timeout |
| **Max clients** | 10 concurrent WebSocket connections |
| **Log** | `/var/log/wcam-ws-relay.log` — 500KB × 3 rotation |

**Playback smoothness evolution:**

| Version | Issue | Fix |
|---------|-------|-----|
| v1 | New `Image()` per frame → async decode reordering → frames drawn out of order (jumping back/forward) | Single reusable `Image` + `frameSeq` counter to discard stale frames |
| v2 | 30fps flood → decode queue buildup → periodic GC pauses every 3-4s → stutter | Relay throttled to ~20fps; `isDecoding` flag prevents concurrent decodes; `rAF` gate syncs to display refresh |

**Trade-off:** ~20fps instead of 30fps, but smooth and consistent across all browsers. The relay adds minimal latency (one extra hop through localhost).

#### Mobile-Friendly Overhaul (9 April 2026)

The entire site has been audited for mobile responsiveness:
- **Viewport meta**: `width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no` — prevents accidental zoom on iOS
- **Touch targets**: All buttons and inputs have `min-height: 44px` (Apple HIG minimum)
- **Modal behavior**: On mobile, modals slide up from bottom (`align-items: flex-end`) — native app feel. On desktop (≥601px), modals are centered.
- **`dvh` units**: `min-height: 100dvh` for proper mobile viewport handling (accounts for browser chrome)
- **Responsive breakpoints**: 600px (tablet/phone), 380px (small phones)
- **Form inputs**: Stack vertically on mobile, side-by-side on desktop. Exception: the Add User form's checkbox + button row stays horizontal on mobile (`form-row-inline`) for a cleaner layout
- **Add User form**: Username and password each get their own full-width row on all screen sizes. Admin checkbox and "Add User" button stay side-by-side on mobile
- **Tables**: Reduced font sizes and padding on mobile
- **`-webkit-tap-highlight-color: transparent`**: Removes blue tap flash on iOS
- **Apple web app meta**: `apple-mobile-web-app-capable`, `apple-mobile-web-app-status-bar-style` for home screen bookmarks

#### Auth Backend — Production WSGI

- **Before:** Flask's built-in `app.run()` dev server — logged every HTTP request, single-threaded
- **After:** Waitress WSGI server — production-grade, 4 threads, 100 connection limit
- Werkzeug request logging suppressed — only auth events (LOGIN_OK/LOGIN_FAIL) written to log
- `secure=True` on session cookies — only sent over HTTPS

#### Services Status

All core services are `enabled` and `active`, configured with `Restart=always` or `Restart=on-failure`:
- `nginx`, `wcam-auth`, `wcam-ws-relay`, `ustreamer-logitech`, `ustreamer-sweex`, `fail2ban`, `ssh`, `cron`

Unnecessary services (bluetooth, avahi-daemon, ModemManager, cloud-init) are already `inactive (dead)` after boot — zero CPU impact.

#### Read-Only Filesystem Resilience (10 April 2026)

On 10 April, the login audit log stopped recording entries after the filesystem went read-only overnight (common on Pi SD cards when ext4 detects journal errors or bad blocks). The `TimedRotatingFileHandler` failed during midnight rotation and entered a permanent broken state — silently dropping all subsequent log entries.

**Root cause:** `OSError: [Errno 30] Read-only file system` during log rotation. The kernel remounted the root filesystem read-only as a protective measure. The filesystem recovered (remounted rw), but the Python logging handler was stuck retrying the failed rotation on every write.

**SD card details:** Generic "SC64G" 64GB card (May 2020), no-name brand. No eMMC health data available (ext_csd not exposed). This is the most likely root cause — cheap cards have poor wear leveling and no power-loss protection.

**What was affected:**
- `/var/log/wcam-login.log` — audit log entries silently dropped for ~15 hours
- All other file-based loggers were equally vulnerable

**Mitigations implemented:**

| Layer | Mechanism | What it protects |
|-------|-----------|-----------------|
| **SafeFileHandler** | Custom logging handler in `auth_server.py` and `ws_relay.py` | Replaces `RotatingFileHandler`/`TimedRotatingFileHandler`. On I/O error: falls back to stderr, closes broken handle, retries on next write. Never enters permanent broken state. |
| **SafeLogger** | Custom logger in `reboot-router.py` | Same pattern — file write with stderr fallback. |
| **save_users() guard** | try/except in `auth_server.py` | Returns `False` on failure, API returns 500 error to caller. |
| **Health check script** | `/usr/local/bin/fs-health-check.sh` (cron, every 5 min) | 4-stage recovery: (1) detect RO, (2) `mount -o remount,rw /`, (3) retry after 5s delay, (4) **forced reboot after 60s** as last resort. Also monitors SD card health (dmesg errors, filesystem state, check interval). |
| **Health endpoint** | `GET /api/health` on wcam-auth | Returns `{filesystem_writable, sd_card: {status, issues}}`. Also logs `SD_CARD_HEALTH` to audit log every hour — **visible in webGUI log viewer**. |
| **systemd ReadWritePaths** | `ReadWritePaths=/var/log` in `wcam-auth.service` | Was `ProtectSystem=strict` with specific file paths — blocked health check's test write. Now allows all of `/var/log`. |

**Auto-recovery flow (health check script):**

```
Every 5 min: touch /var/log/.fs_health_test
  ├── Writable → check SD card health (dmesg errors, fs state, check interval)
  │              └── Log warning/critical to journal if issues found
  └── Read-only → Stage 1: mount -o remount,rw /
                   ├── Success → restart all services → done
                   └── Failed  → Stage 2: retry after 5s delay
                                   ├── Success → restart all services → done
                                   └── Failed  → Stage 3: forced reboot in 60s
                                                   (reboot marker prevents repeat for 1h)
```

**SD card health monitoring:**

| Check | Source | Warning threshold | Critical threshold |
|-------|--------|-------------------|-------------------|
| mmc0 errors | `dmesg -T` | Any errors with "error/timeout/reset/CRC" | Any "I/O error" or "Buffer I/O error" |
| Filesystem state | `tune2fs -l` | — | State != "clean" |
| Last checked | `tune2fs -l` | >180 days since last fsck | — |
| Reboot marker | `/var/log/.fs_recovery_reboot_pending` | Marker exists (<1h old) | — |

**SD card health in webGUI:** The `SD_CARD_HEALTH` event appears in the login log viewer (Admin → Login Log tab) every hour. Shows `status=ok` when healthy, or `status=warning/critical issues=...` when problems detected.

**What's still vulnerable:**
- **nginx** — if filesystem goes read-only, nginx will return 500 errors for log writes. No fallback.
- **fail2ban** — same issue. Would stop banning but existing bans persist.
- **Shell scripts** (`namecheap-ddns.sh`, `sweex-capture.sh`) — would fail silently. Sweex writes to tmpfs so is unaffected. DDNS log writes would be lost.
- **SD card replacement** — the underlying hardware issue is not addressed. The card is a generic no-name brand from 2020. If read-only events recur, replace with an industrial/high-endurance SD card (e.g. Samsung PRO Endurance, Kingston Industrial).

### Security Notes (9 April 2026 — Hardened)

- **HTTPS enabled** — Let's Encrypt TLS on port 443, HTTP redirects to HTTPS, HSTS header set
- **Session-based auth** — Custom login page replaces Basic Auth (less conspicuous to scanners)
- **Role-based access** — Admin users can manage users via UI; non-admin users have view-only access
- **Login audit log** — All logins, logouts, and user changes logged with timestamp, username, and IP (1 year retention)
- **Last-admin guard** — Cannot delete the final admin user
- **fail2ban** — SSH jail (3 failed attempts → 1h ban), wcam-auth jail (5 failed logins → 1h ban)
- **SSH hardening** (`/etc/ssh/sshd_config.d/99-hardening.conf`):
  - `MaxAuthTries 3` (was 6)
  - `LoginGraceTime 30s` (was 2m)
  - `X11Forwarding no` (was yes)
  - `ClientAliveInterval 300` — disconnects idle sessions after 10 min
  - `MaxSessions 5` (was 10)
  - `PasswordAuthentication yes` — password login still works
- **nginx rate limiting** — 10 req/s per IP on port 443 (burst 20), applied to page loads and image requests, not the MJPEG stream
- **nginx security headers** — `X-Frame-Options`, `X-Content-Type-Options`, `X-XSS-Protection`, `Referrer-Policy`
- **User database uses bcrypt** — `$2y$` hashes in `/etc/nginx/.wcam-users.json` (migrated from `.htpasswd` on 9 April)
- **Session cookies** — httponly, samesite=Lax, 24h TTL, secure random tokens
- **Auth backend** — Flask on `127.0.0.1:8086` only, not internet-facing
- **No firewall** (`ufw` not installed) — ports 8080 and 8085 are exposed to the internet as well as LAN
- **camviewer.service masked** — conflicting Flask server on port 8085 removed and masked

### Key Files

| File | Purpose |
|------|---------|
| `/etc/nginx/sites-available/camviewer` | nginx config — session cookie auth via `auth_request`, rate limiting, 3 server blocks, no-cache on login page |
| `/var/www/camviewer/login.html` | Login page — logo, username/password form, dark themed, `window.location.replace('/')` on success |
| `/var/www/camviewer/index.html` | Dashboard — admin Users button, login log viewer, Safari WebSocket canvas stream, mobile-responsive |
| `/var/www/camviewer/ws_relay.py` | WebSocket MJPEG relay — asyncio server, reads ustreamer stream, broadcasts JPEG frames to WS clients |
| `/var/www/camviewer/auth_server.py` | Waitress WSGI server — bcrypt auth, session management, user CRUD API, login audit logging |
| `/etc/nginx/.wcam-users.json` | User credentials (bcrypt `$2y$` hashes) + admin flags — read/written by auth backend |
| `/etc/nginx/.htpasswd` | 🚫 **Legacy** — replaced by `.wcam-users.json` on 9 April 2026 |
| `/var/log/wcam-auth.log` | Auth backend log — LOGIN_OK/LOGIN_FAIL entries for fail2ban (app-level rotation, 1MB × 5) |
| `/var/log/wcam-login.log` | Login audit log — JSON lines, daily rotation, 365 days retention (1 year) |
| `/etc/systemd/system/wcam-auth.service` | Systemd service for Waitress WSGI auth backend |
| `/etc/systemd/system/wcam-ws-relay.service` | Systemd service for WebSocket MJPEG relay |
| `/etc/ssh/sshd_config.d/99-hardening.conf` | SSH hardening drop-in (MaxAuthTries, LoginGraceTime, X11Forwarding, etc.) |
| `/etc/fail2ban/jail.local` | fail2ban jail config — SSH and wcam-auth ban rules |
| `/etc/fail2ban/filter.d/wcam-auth.conf` | fail2ban filter — matches `LOGIN_FAIL` entries |
| `/etc/letsencrypt/renewal/wansteadcam.lumoco.com.conf` | Certbot renewal config (webroot method) |
| `/etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh` | Hook to reload nginx after cert renewal |
| `/var/www/certbot/` | Webroot for Let's Encrypt ACME challenge files |
| `/var/www/camviewer/sweex_latest.jpg` | Symlink → `/var/www/camviewer/sweex-tmpfs/sweex_latest.jpg` (tmpfs, zero SD writes) |
| `/var/www/camviewer/sweex-tmpfs/` | tmpfs mount (2MB) — Sweex image lives in RAM |
| `/etc/systemd/system/ustreamer-logitech.service` | Logitech camera service — `--slowdown` flag for idle CPU reduction |
| `/etc/systemd/system/ustreamer-sweex.service` | Sweex still capturer service |
| `/usr/local/bin/sweex-capture.sh` | Capture loop: v4l2-ctl -w → raw RGB → ffmpeg → JPEG (writes to tmpfs) |
| `/usr/local/bin/namecheap-ddns.sh` | Dynamic DNS updater for wansteadcam.lumoco.com |
| `/usr/local/bin/reboot-router.py` | Router reboot script — authenticates, triggers reboot, verifies recovery |
| `/var/log/router-reboot.log` | Router reboot history — timestamps, success/failure |
| `/etc/logrotate.d/wcam-auth` | Logrotate config for auth log (safety net) |
| `/etc/logrotate.d/router-reboot` | Logrotate config for router reboot log |
| `/etc/logrotate.d/namecheap-ddns` | Logrotate config for DDNS log |
| `/etc/systemd/journald.conf.d/size-limit.conf` | systemd journal size limits (50M persistent, 20M runtime, 1 month max) |
| `/usr/local/bin/fs-health-check.sh` | Filesystem health check — detects read-only FS, auto-remounts rw, restarts services |
| `/var/www/camviewer/auth_server.py` | Waitress WSGI server — includes `SafeFileHandler` (crash-proof logging), `/api/health` endpoint, `save_users()` error handling |
| `/var/www/camviewer/ws_relay.py` | WebSocket MJPEG relay — includes `SafeRotatingHandler` (crash-proof logging) |

---

## Useful Commands

```bash
# Check connected USB devices
lsusb

# List video devices
v4l2-ctl --list-devices

# List camera formats
v4l2-ctl -d /dev/video0 --list-formats-ext

# Capture a Logitech still
fswebcam -d /dev/video0 -r 640x480 --no-banner /tmp/capture.jpg

# Capture a Sweex still manually (libv4l2 wrapper required)
v4l2-ctl -w -d /dev/video2 --set-fmt-video=width=176,height=144,pixelformat=RGB3 \
  --stream-mmap --stream-count=5 --stream-to=/tmp/sweex.raw
# Then convert last frame:
python3 -c "d=open('/tmp/sweex.raw','rb').read(); open('/tmp/frame.raw','wb').write(d[-76032:])"
ffmpeg -y -f rawvideo -pixel_format rgb24 -video_size 176x144 -i /tmp/frame.raw /tmp/sweex.jpg

# Check service status
sudo systemctl status ustreamer-logitech ustreamer-sweex nginx fail2ban sshd wcam-auth

# View logs
sudo journalctl -u ustreamer-logitech -f
sudo journalctl -u ustreamer-sweex -f
sudo journalctl -u wcam-auth -f

# fail2ban
sudo fail2ban-client status                    # List all jails
sudo fail2ban-client status sshd               # SSH jail status
sudo fail2ban-client status wcam-auth          # Auth backend jail status
sudo fail2ban-client set sshd unbanip <IP>     # Unban an IP
sudo fail2ban-client set wcam-auth unbanip <IP>  # Unban an IP from auth jail

# Auth backend
sudo systemctl status wcam-auth                # Check auth service
sudo cat /var/log/wcam-auth.log                # View auth log (LOGIN_OK / LOGIN_FAIL)
sudo cat /var/log/wcam-login.log               # View login audit log (JSON lines, 1 year)
sudo journalctl -u wcam-auth -f                # Follow auth service journal

# User management (via API, admin only)
curl -sk -b /tmp/cookies.txt http://127.0.0.1:8086/api/users                    # List users
curl -sk -b /tmp/cookies.txt -X POST http://127.0.0.1:8086/api/users \          # Create user
  -H 'Content-Type: application/json' \
  -d '{"username":"newuser","password":"pass123","is_admin":false}'
curl -sk -b /tmp/cookies.txt -X PUT http://127.0.0.1:8086/api/users/newuser \   # Edit user
  -H 'Content-Type: application/json' \
  -d '{"username":"newuser","is_admin":true}'
curl -sk -b /tmp/cookies.txt -X DELETE http://127.0.0.1:8086/api/users/newuser  # Delete user
cat /etc/nginx/.wcam-users.json                  # View raw user database

# Audit log (via API, admin only)
curl -sk -b /tmp/cookies.txt "http://127.0.0.1:8086/api/audit?page=1&per_page=20"  # Recent entries
curl -sk -b /tmp/cookies.txt "http://127.0.0.1:8086/api/audit?event=LOGIN_FAIL"     # Failed logins only
curl -sk -b /tmp/cookies.txt "http://127.0.0.1:8086/api/audit?username=gduthie"     # Specific user

# Dynamic DNS
sudo /usr/local/bin/namecheap-ddns.sh   # Run manually
cat /var/log/namecheap-ddns.log          # View DDNS update history
cat /var/run/namecheap-ddns-ip           # Last known public IP

# Router reboot
sudo /usr/local/bin/reboot-router.py          # Reboot the router (live)
sudo /usr/local/bin/reboot-router.py --dry-run  # Test without rebooting
cat /var/log/router-reboot.log                 # View reboot history
sudo crontab -l | grep reboot                  # Check scheduled reboot time

# Test public access (login page should be visible without auth)
curl -sk https://wansteadcam.lumoco.com/login.html -o /dev/null -w "%{http_code}"

# Test login via API
curl -sk -c /tmp/cookies.txt -X POST https://wansteadcam.lumoco.com/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"gduthie","password":"<your-password>"}'

# Test dashboard with session cookie
curl -sk -b /tmp/cookies.txt https://wansteadcam.lumoco.com/ -o /dev/null -w "%{http_code}"

# Test HTTP→HTTPS redirect
curl -s -o /dev/null -w "%{http_code} → %{redirect_url}" http://wansteadcam.lumoco.com/

# Check certificate expiry
echo | openssl s_client -connect wansteadcam.lumoco.com:443 -servername wansteadcam.lumoco.com 2>/dev/null | openssl x509 -noout -dates

# Test certbot renewal (dry run)
sudo certbot renew --dry-run

# Manually force certbot renewal
sudo certbot renew --force-renewal

# Check disk usage and log sizes
df -h / && du -sh /var/log/ /var/log/* 2>/dev/null | sort -rh | head -10

# Check systemd journal size
journalctl --disk-usage

# Check tmpfs mounts
mount | grep tmpfs

# Check ustreamer client connections
sudo journalctl -u ustreamer-logitech --since "1 minute ago" | grep "clients now"

# Force log rotation (test)
sudo logrotate -f /etc/logrotate.d/wcam-auth

# Filesystem health check
sudo /usr/local/bin/fs-health-check.sh          # Manual run
sudo journalctl -t fs-health-check --since "1 hour ago"  # View health check logs
curl -s http://127.0.0.1:8086/api/health         # Check filesystem status via API
```
