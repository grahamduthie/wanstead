# WagtailCam — Raspberry Pi Security Camera Project

---

## Deployment Guide (IMPORTANT - READ THIS FIRST)

### Code Location
- **Mac source directory**: `/Users/gduthie/Programming/Wagtailcam/wanstead/`
- **Pi files**: `/var/www/camviewer/` (web files), `/usr/local/bin/` (scripts)

### Typical Workflow
1. Edit files on Mac in the source directory
2. Test locally if possible
3. Deploy to Pi using scp and ssh

### Deploy Commands

**Single file:**
```bash
# From Mac:
scp files/var_www_camviewer/auth_server.py gduthie@wagtailcam.gdx.org.uk:/tmp/
scp files/var_www_camviewer/timelapse.html gduthie@wagtailcam.gdx.org.uk:/tmp/
scp files/usr/local/bin/capture_timelapse.py gduthie@wagtailcam.gdx.org.uk:/tmp/

# SSH to Pi and copy:
ssh gduthie@wagtailcam.gdx.org.uk
sudo cp /tmp/auth_server.py /var/www/camviewer/
sudo cp /tmp/timelapse.html /var/www/camviewer/
sudo cp /tmp/capture_timelapse.py /usr/local/bin/
sudo systemctl restart wcam-auth
```

**After deploying, restart service:**
```bash
ssh gduthie@wagtailcam.gdx.org.uk
sudo systemctl restart wcam-auth
sudo systemctl restart wcam-ws-relay
```

### Testing Endpoints (from Pi)
```bash
# Test auth login
TOKEN=$(curl -s -c /tmp/c.txt -X POST http://localhost:8086/api/login -H "Content-Type: application/json" -d '{"username":"gduthie","password":"cheshunt"}' | grep -o '"ok":true')
echo "Login: $TOKEN"

# Test timelapse dates
curl -s -b /tmp/c.txt http://localhost:8086/api/timelapse/dates

# Test MJPEG stream
curl -s -b /tmp/c.txt "http://localhost:8086/api/timelapse/stream?date=2026-04-16&speed=300" | head -5
```

---

## Overview

WagtailCam is a single-camera security camera system running on a Raspberry Pi. It provides live MJPEG streaming via a web interface with PTZ (Pan-Tilt-Zoom) controls for the Logitech PTZ Pro 2 camera.

This project is a modified derivative of the [WansteadCam project](https://github.com/grahamduthie/wanstead), adapted for single-camera operation with PTZ support.

**Status:** Live at https://wagtailcam.gdx.org.uk

**Quick Links:**
- [Live Camera](https://wagtailcam.gdx.org.uk/) - Main viewer
- [Timelapse](https://wagtailcam.gdx.org.uk/timelapse.html) - View captured timelapse

| URL | Purpose |
|-----|---------|
| https://wagtailcam.gdx.org.uk | External access (HTTPS, auth required) |
| http://wagtailcam.local | Local access (auth required, auto-upgrades to HTTPS) |

**SSL Certificate:** Let's Encrypt (auto-renews via certbot.timer, runs ~1:10 AM daily)

**Git Repository:** Initial commit made. Code on Mac at `/Users/gduthie/Programming/Wagtailcam/wanstead/`

### Pi Access

- **Hostname**: wagtailcam.gdx.org.uk
- **SSH**: `ssh gduthie@wagtailcam.gdx.org.uk`
- **User**: gduthie
- **Key**: Uses SSH keys (no password auth)

---

## Hardware

| Component | Detail |
|-----------|--------|
| Device | Raspberry Pi 3 Model B Plus (aarch64, Debian) |
| Camera | Logitech PTZ Pro 2 (`/dev/video0`) |
| Camera Resolution | 1920x1080 @ 30fps (MJPEG) |
| Camera PTZ Controls | Pan, Tilt, Zoom, Focus via V4L2 |

### Camera Technical Details

The Logitech PTZ Pro 2 is detected as `/dev/video0` and `/dev/video1` via the UVC (USB Video Class) driver. V4L2 controls available:

| Control | Range | Notes |
|---------|-------|-------|
| pan_speed | -1 to 1 | Speed only, no absolute position |
| tilt_speed | -1 to 1 | Speed only, no absolute position |
| zoom_absolute | 100-1000 | 1.0x to 10x zoom |
| focus_absolute | 0-255 | Manual focus (0 = auto) |
| focus_automatic_continuous | 0/1 | Enable/disable auto focus |
| auto_exposure | 0-3 | Auto (Aperture Priority) |
| gain | 0-255 | Currently at max (255) for low light |
| backlight_compensation | 0-1 | On (1) for window scenes |

**Note:** PTZ Pro 2 has speed controls only (pan_speed, tilt_speed), not absolute position controls. However, the camera has built-in presets and home position accessible via UVC Extension Unit commands (see Camera Presets section below).

**Low Light Considerations:** The PTZ Pro 2 is a consumer camera, not a true security camera. For window scenes at night:
- Performance will be limited
- Consider external IR illumination for better night vision
- Current settings maximize gain for low light

### Camera Presets

The PTZ Pro 2 has built-in preset functionality accessible via UVC Extension Unit commands. The system uses a **reference-based approach** for maximum accuracy:

| Preset | Save Command | Recall Command |
|--------|--------------|----------------|
| Preset 1 | Save current position | Go to Preset 1 |
| Preset 2 | Save current position | Go to Preset 2 |
| Preset 3 | Save current position | Go to Preset 3 |
| Home | - | Return to home position (pan/tilt only) |

**Implementation:** Uses `/usr/local/bin/ptz-preset` binary which sends UVC Extension Unit commands (Unit ID 11, Selector 0x02) directly to the camera firmware.

**Workflow:**
- **Configure**: Camera goes home (resets pan/tilt/zoom) → User manually positions camera → Tap preset to save
- **Recall**: Camera goes home (resets pan/tilt/zoom) → Camera moves to saved preset position
- **Home**: Camera returns to home position (pan/tilt only, zoom unchanged)

**Preset Naming:** Right-click (or long-press on mobile) any preset button to assign a custom name and focus value. Stored in `/var/www/camviewer/.preset_names.json`.

**Preset Focus Setting:** Each preset can have a fixed focus value (0-255) for consistent timelapse captures. Set "Use Current" to capture the camera's current focus, or leave empty for auto-focus.

**Note:** Presets save the complete camera state including pan, tilt, zoom, and focus positions.

---

## Software Architecture

```
Client Browser
     │
     ▼ HTTPS (443)
nginx (TLS + auth)
     │
     ├─► /api/* ──────────────────► wcam-auth (Flask :8086) ─ User auth, PTZ control
     │
     └─► /stream/* ────────────────► ustreamer (:8080) ─ MJPEG stream
     │
     └─► /ws/stream/* ────────────► wcam-ws-relay (:8087) ─ WebSocket relay (~20fps)
```

### Components

| Component | Technology | Purpose |
|-----------|------------|---------|
| nginx | Reverse Proxy | TLS termination, session auth, routing |
| wcam-auth | Python 3 + Flask + Waitress | Authentication, user management, PTZ API |
| ustreamer | C | High-performance MJPEG streaming |
| wcam-ws-relay | Python 3 + asyncio + websockets | WebSocket MJPEG relay for cross-browser playback |

---

## Project Structure

```
wanstead/
├── README.md                    # Original WansteadCam documentation
├── WAGTAILCAM.md               # This file
├── files/
│   ├── usr_local_bin/
│   │   └── ptz-preset.c       # C helper for camera preset commands (UVC XU)
│   ├── etc_nginx/
│   │   └── camviewer           # nginx site config (HTTPS + auth + no-cache)
│   ├── etc_systemd/
│   │   ├── ustreamer.service   # uStreamer systemd service (1080p MJPEG)
│   │   ├── wcam-auth.service   # Auth backend systemd service
│   │   └── wcam-ws-relay.service # WebSocket relay systemd service
│   └── var_www_camviewer/     # Web application
│       ├── index.html          # Main camera viewer (single cam + PTZ + presets)
│       ├── login.html          # Login page
│       ├── auth_server.py      # Flask auth backend + PTZ + preset endpoints
│       ├── ws_relay.py        # WebSocket MJPEG relay
│       ├── config.json.example # Configuration template
│       └── WagtailCam.png     # Logo image
```

---

## Modifications from WansteadCam

The following changes were made to adapt WansteadCam for WagtailCam:

### 1. Single Camera
- Removed Sweex Mini Webcam card from `index.html`
- Deleted `etc_systemd/ustreamer-sweex.service`
- Simplified nginx config to single camera paths

### 2. PTZ Controls Added
- Added PTZ control panel to `index.html`:
  - D-pad for pan/tilt (hold to move, release to stop)
  - Zoom slider (1.0x - 10x)
  - Focus slider (Auto/Manual)
  - PTZ state syncs every 5 seconds across browsers
- Added `/api/ptz` endpoint to `auth_server.py` using `v4l2-ctl`
- Added `/api/ptz/state` endpoint to read current camera state
- **Admin-only access:** PTZ controls only visible to admin users

### 3. Logo
- Custom WagtailCam.png logo in project directory
- References updated from WansteadCam.png to WagtailCam.png

### 4. Names Changed
- "WansteadCam" → "WagtailCam" throughout
- `/ws/logitech/` → `/ws/stream/`
- `/stream/logitech/` → `/stream/`

### 5. Router Reboot Removed
- Deleted `usr_local_bin/reboot-router.py`
- Removed ROUTER_REBOOT_* log event filters from admin UI

### 6. Stream Resolution
- Changed from 640x480 to 1920x1080 in ustreamer config

### 7. Security Improvements
- nginx auth_request protection on all pages and streams
- Session cookie with Secure, HttpOnly, SameSite flags
- No-cache headers on protected pages to prevent cached content after logout
- **File-backed sessions** - Sessions persist across server restarts
- **Login rate limiting** - Max 10 failed attempts per 5 minutes per IP
- **Bcrypt 12 rounds** - OWASP 2026 recommended work factor
- **JPEG validation** - Timelapse captures verified before saving
- **Error logging** - Silent failures logged at debug level

### 8. Mobile/Touch Improvements
- `touch-action: manipulation` on PTZ buttons
- `preventDefault()` on touch events
- `ontouchcancel` handler for interrupted touches
- Removed `:hover` CSS (causes issues on mobile)

---

## Deployment Instructions

### Prerequisites
```bash
sudo apt update
sudo apt install -y nginx python3-pip certbot ustreamer v4l-utils fail2ban
sudo pip3 install --break-system-packages flask websockets waitress bcrypt
```

### 1. Deploy configuration files
```bash
# Compile and install PTZ preset helper
sudo mkdir -p /usr/local/bin
gcc -o /usr/local/bin/ptz-preset files/usr_local_bin/ptz-preset.c
sudo cp /usr/local/bin/ptz-preset /usr/local/bin/
sudo chmod +x /usr/local/bin/ptz-preset

# Copy nginx config
sudo cp files/etc_nginx/camviewer /etc/nginx/sites-available/wagtailcam
sudo ln -sf /etc/nginx/sites-available/wagtailcam /etc/nginx/sites-enabled/

# Remove default site
sudo rm -f /etc/nginx/sites-enabled/default

# Copy systemd services
sudo cp files/etc_systemd/*.service /etc/systemd/system/

# Create web directory and copy files
sudo mkdir -p /var/www/camviewer /var/www/certbot /var/www/camviewer/.sessions
sudo cp -r files/var_www_camviewer/* /var/www/camviewer/
sudo chmod +x /var/www/camviewer/*.py
sudo chown -R gduthie:gduthie /var/www/camviewer

# Reload systemd
sudo systemctl daemon-reload
```

### 2. Create user database
```bash
sudo python3 -c "
import bcrypt, json
users = {
    'admin': {'hash': bcrypt.hashpw(b'your-password', bcrypt.gensalt(rounds=12)).decode(), 'is_admin': True}
}
with open('/etc/nginx/.wcam-users.json', 'w') as f:
    json.dump(users, f, indent=2)
"
```

**Note:** Bcrypt uses 12 rounds (OWASP 2026 recommendation) for secure password hashing. Account creation via the web UI automatically uses this work factor.

### 3. Set up SSL certificate
```bash
# Configure DNS to point wagtailcam.gdx.org.uk to your external IP
# Open ports 80 and 443 on firewall

# Test HTTP access first
curl http://wagtailcam.gdx.org.uk/

# Get certificate
sudo certbot certonly --webroot -w /var/www/certbot -d wagtailcam.gdx.org.uk \
  --non-interactive --agree-tos -m your-email@example.com

# Update nginx config with SSL paths, then reload
sudo nginx -t && sudo systemctl reload nginx
```

### 4. Start services
```bash
sudo systemctl enable --now ustreamer wcam-auth wcam-ws-relay nginx
```

---

## Services Running on Pi

| Service | Port | Purpose |
|---------|------|---------|
| nginx | 80, 443 | Web server, TLS, reverse proxy |
| ustreamer | 8080 | MJPEG stream source |
| wcam-auth | 8086 | Auth, user management, PTZ API |
| wcam-ws-relay | 8087 | WebSocket MJPEG relay |
| capture_timelapse | cron | Captures frames to NAS every 5 min |

---

## Network Storage (NAS)

The Pi mounts a share from a network NAS for storing timelapse captures and other data.

| Setting | Value |
|---------|-------|
| NAS | nas-A9-C2-58 (172.16.10.107) |
| Share | //172.16.10.107/media/WagtailCam |
| Mount Point | /mnt/nas |
| Access | Guest read/write |
| Protocol | CIFS/SMB v1.0 |

**Mount Configuration:**
- Configured in `/etc/fstab` for automatic mounting at boot
- Uses `_netdev` option to wait for network before mounting
- SMB version 1.0 required for compatibility with older NAS

**Commands:**
```bash
# Manually mount
sudo mount -a

# Check mount
ls /mnt/nas

# Unmount (if needed)
sudo umount /mnt/nas
```

---

## Timelapse Capture System

The system automatically captures frames from Preset 1 position for timelapse viewing.

### Configuration

| Setting | Value |
|---------|-------|
| Capture interval | Every 5 minutes |
| Storage location | /mnt/nas/timelapse/YYYY-MM-DD/ |
| Preset used | Preset 1 |
| Daylight hours | 30 min before sunrise to 30 min after sunset |
| Location | Twyford, Berkshire, UK (51.48°N, 1.0°W) |
| Image size | 1920x1080 JPEG |

### Capture Script

`/usr/local/bin/capture_timelapse.py` - Python script that:
1. Calculates sunrise/sunset for current day using `astral` library
2. Only captures during daylight hours
3. Moves camera to Preset 1 (via home + preset recall)
4. Waits for camera to settle
5. Captures frame from ustreamer's `/snapshot` endpoint
6. Saves to `/mnt/nas/timelapse/YYYY-MM-DD/HHMMSS.jpg`

### Cron Job

Runs every 5 minutes via root crontab:
```
*/5 * * * * /usr/local/bin/capture_timelapse.py >> /var/log/timelapse.log 2>&1
```

### Dependencies

```bash
pip3 install --break-system-packages astral pytz
sudo pip3 install --break-system-packages astral pytz  # For cron (runs as root)
```

### Commands

```bash
# Test capture manually
/usr/local/bin/capture_timelapse.py

# Check log
cat /var/log/timelapse.log

# View captured images
ls -la /mnt/nas/timelapse/2026-04-16/

# Check capture status
cat /var/www/camviewer/.capture_status.json

# Stop capturing (remove cron)
/usr/bin/crontab -r  # careful!
```

### User Experience

When a capture occurs while a user is viewing the live stream:
1. Overlay appears on camera feed: "📷 Capturing..."
2. PTZ controls are temporarily disabled
3. Camera moves to Preset 1 position
4. Frame is captured
5. Brief "✓ Captured" confirmation shown
6. PTZ controls re-enabled

This ensures users are informed and prevents PTZ conflicts during capture.

---

## Timelapse Viewer

Access the timelapse viewer at: **https://wagtailcam.gdx.org.uk/timelapse.html**

### Features

- Calendar view showing dates with available images (green dots)
- Yesterday/Today quick buttons
- "Other Date" button to show/hide calendar for selecting other dates
- **On-demand download**: Preview image shown first, click "Download All" to preload all images
- Progress indicator during download
- Frame-by-frame navigation with arrow keys or buttons
- Playback at adjustable speed (1x, 3x, 5x, 10x)
- Progress bar for quick seeking
- Reset button to restart from beginning

### On-Demand Image Preloading

To improve performance on slow/unreliable connections:
1. Selecting a date shows a random preview image
2. "Download All (N)" button initiates background download of all images
3. Progress bar shows download status
4. Once complete (~50MB for a full day), playback is instant
5. Images are cached in browser memory for smooth navigation

**Bandwidth usage**: ~50MB per day of timelapse (168 images × ~300KB each)

### MJPEG Streaming

The `/api/timelapse/stream` endpoint streams timelapse as MJPEG. It is used by the Joggler kiosk dashboard (see [Joggler Kiosk Integration](#joggler-kiosk-integration) below). The web viewer (`timelapse.html`) uses JS frame cycling with on-demand preloading instead — this allows frame-accurate seeking and progress bars that MJPEG cannot provide.

### Timelapse API Endpoints

```
GET /api/timelapse/dates                                      # List dates with images
GET /api/timelapse/list?date=YYYY-MM-DD                       # List images for a date
GET /api/timelapse/image?path=...                             # Serve a specific image
GET /api/timelapse/stream?date=YYYY-MM-DD&speed=500&start=0  # MJPEG stream
```

---

## API Reference

### PTZ Control API

```
GET /api/ptz?pan=-1           # Pan left (speed -1)
GET /api/ptz?pan=1            # Pan right (speed 1)
GET /api/ptz?pan=0            # Stop pan
GET /api/ptz?tilt=-1          # Tilt down
GET /api/ptz?tilt=1           # Tilt up
GET /api/ptz?tilt=0           # Stop tilt
GET /api/ptz?zoom=200         # Set zoom to 2.0x
GET /api/ptz?focus=128        # Set focus to 128
GET /api/ptz?focus_auto=1     # Enable auto focus
GET /api/ptz?focus_auto=0     # Disable auto focus
```

Multiple parameters can be combined:
```
/api/ptz?pan=0&tilt=0         # Stop all movement
/api/ptz?zoom=500&focus=0     # Zoom to 5x, auto focus
```

### PTZ State API

```
GET /api/ptz/state             # Returns current zoom, focus, focus_auto values
```

### Preset Control API

```
GET  /api/preset/names          # Get all preset names
PUT  /api/preset/name/1         # Set name for Preset 1 {"name": "Garden View"}
POST /api/preset/home           # Move camera to home position
POST /api/preset/save/1        # Save current position as Preset 1
POST /api/preset/save/2        # Save current position as Preset 2
POST /api/preset/save/3        # Save current position as Preset 3
POST /api/preset/recall/1      # Go to Preset 1
POST /api/preset/recall/2      # Go to Preset 2
POST /api/preset/recall/3      # Go to Preset 3
```

Used by browser to sync PTZ state every 5 seconds.

### Other API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/login` | POST | Authenticate |
| `/api/verify` | GET | Validate session |
| `/api/health` | GET | Health check |
| `/api/me` | GET | Current user info |
| `/api/logout` | POST | Logout |
| `/api/users` | GET/POST | List/Create users (admin) |
| `/api/users/<name>` | PUT/DELETE | Update/Delete user (admin) |
| `/api/audit` | GET | Audit log (admin) |

---

## Firewall Configuration

External access via https://wagtailcam.gdx.org.uk requires:
- Port 80 → Pi port 80 (for Let's Encrypt certbot)
- Port 443 → Pi port 443 (HTTPS)

---

## Known Issues / Troubleshooting

### WiFi Connectivity
The Pi may occasionally lose local network connectivity. If local access fails but external access works:
```bash
sudo ip link set wlan0 down && sleep 2 && sudo ip link set wlan0 up
```

### Camera Low Light
The PTZ Pro 2 is a consumer camera. For window scenes at night:
- Performance will be limited
- Consider dedicated security camera with IR LEDs for night vision
- Current gain is set to maximum (255) for low light

---

## Joggler Kiosk Integration

The O2 Joggler home dashboard (`/Users/gduthie/Programming/Joggler/dashboard.html`) connects to WagtailCam from any network using token authentication. The Joggler runs as a local `file://` kiosk and cannot use browser session cookies.

### Token Authentication

A static 40-character hex token (`JOGGLER_TOKEN`) is defined in `auth_server.py`. The `require_login` decorator checks this token in the query string before requiring a session cookie:

```python
if request.args.get("token") == JOGGLER_TOKEN:
    return f(*args, **kwargs)
```

The same token is embedded in `dashboard.html` as `WCAM_TOKEN`. All Joggler API calls use the `wcamUrl(path)` helper, which appends `?token=TOKEN` or `&token=TOKEN` depending on whether the path already has a query string.

The LAN IP bypass (`172.16.10.*`) remains active — token auth is additive.

### Live Stream Proxy (`/api/live`)

ustreamer binds to `http://127.0.0.1:8080` and is not directly internet-accessible. The `/api/live` Flask endpoint proxies it:

- Protected by `@require_login` (token accepted)
- Streams MJPEG via a chunked generator — browser holds only the current frame
- Returns 503 if ustreamer is unavailable

nginx proxies `/api/live` to Flask with `proxy_buffering off` and `proxy_read_timeout 3600s`. The Joggler reconnects every 5 minutes to keep the stream healthy.

### Timelapse Stream

The Joggler requests `/api/timelapse/stream?date=YYYY-MM-DD&speed=500&start=N`:

- `speed=500`: 500ms per frame (suited to the Joggler's Atom CPU)
- `start=N`: frame index to begin from, enabling resume-after-pause

A separate 520ms JS tick updates the time display independently without parsing the MJPEG stream.

### nginx Security Additions

**Token stripping from access logs** (`nginx.conf`): The `no_query` log format uses `$uri` instead of `$request_uri`, preventing auth tokens from appearing in log files:

```nginx
log_format no_query '$remote_addr - $remote_user [$time_local] '
                    '"$request_method $uri $server_protocol" '
                    '$status $body_bytes_sent '
                    '"$http_referer" "$http_user_agent"';
```

The WagtailCam site writes to a separate log using this format:

```nginx
access_log /var/log/nginx/wagtailcam.log no_query;
```

**Rate limiting on `/api/live`**: Prevents the proxied stream from being used as a bandwidth amplifier. Defined in `nginx.conf`:

```nginx
limit_req_zone $binary_remote_addr zone=wcam_live:1m rate=2r/m;
```

Applied in the `camviewer` site config:

```nginx
location = /api/live {
    limit_req zone=wcam_live burst=2 nodelay;
    proxy_pass http://127.0.0.1:8086;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
    ...
}
```

Allows 2 new connections per minute per IP with a burst of 2.

---

## Notes for Future LLM Sessions

When continuing this project:

1. **SSH Access**: `ssh gduthie@wagtailcam.gdx.org.uk` (external access via Tailscale or similar)

2. **Service Management**:
   ```bash
   systemctl status nginx ustreamer wcam-auth wcam-ws-relay
   systemctl restart nginx ustreamer wcam-auth wcam-ws-relay
   ```

3. **Logs**:
   ```bash
   tail -f /var/log/wcam-auth.log
   tail -f /var/log/wcam-ws-relay.log
   journalctl -u wcam-auth -f
   ```

4. **Camera Troubleshooting**:
   ```bash
   v4l2-ctl --list-devices          # Check camera detection
   v4l2-ctl -d /dev/video0 --list-ctrls   # List available controls
   ```

5. **PTZ Preset Testing**:
   ```bash
   /usr/local/bin/ptz-preset home      # Go to home position
   /usr/local/bin/ptz-preset save1      # Save Preset 1
   /usr/local/bin/ptz-preset preset1     # Go to Preset 1
   ```

6. **Timelapse**:
   ```bash
   # Test capture manually
   /usr/local/bin/capture_timelapse.py
   
   # Check log
   cat /var/log/timelapse.log
   
   # View captures on NAS
   ls /mnt/nas/timelapse/
   
   # Check/manage cron
   sudo crontab -l
   ```

7. **SSL Certificate**:
   - Certificate stored in `/etc/letsencrypt/live/wagtailcam.gdx.org.uk/`
   - Auto-renews daily via certbot.timer
   - Manual renew: `sudo certbot renew`

8. **Web Files**: `/var/www/camviewer/` (index.html, login.html, timelapse.html, auth_server.py, ws_relay.py)

9. **Git**: Code is tracked in git on the Mac at `/Users/gduthie/Programming/Wagtailcam/wanstead/`

---

## References

- [WansteadCam GitHub](https://github.com/grahamduthie/wanstead)
- [uStreamer](https://github.com/pikvm/ustreamer) - MJPEG streamer
- [V4L2 Controls](https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/v4l2.html) - Linux video device API
- [Let's Encrypt](https://letsencrypt.org/) - Free SSL certificates
