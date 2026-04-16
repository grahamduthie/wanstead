# WagtailCam — Raspberry Pi Security Camera Project

## Overview

WagtailCam is a single-camera security camera system running on a Raspberry Pi. It provides live MJPEG streaming via a web interface with PTZ (Pan-Tilt-Zoom) controls for the Logitech PTZ Pro 2 camera.

This project is a modified derivative of the [WansteadCam project](https://github.com/grahamduthie/wanstead), adapted for single-camera operation with PTZ support.

**Status:** Live at https://wagtailcam.gdx.org.uk

| URL | Purpose |
|-----|---------|
| https://wagtailcam.gdx.org.uk | External access (HTTPS, auth required) |
| http://wagtailcam.local | Local access (auth required, auto-upgrades to HTTPS) |

**SSL Certificate:** Let's Encrypt (auto-renews via certbot.timer, runs ~1:10 AM daily)

**Git Repository:** Initial commit made. Code on Mac at `/Users/gduthie/Programming/Wagtailcam/wanstead/`

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

The PTZ Pro 2 has built-in preset functionality accessible via UVC Extension Unit commands:

| Preset | Save Command | Recall Command |
|--------|--------------|----------------|
| Preset 1 | Save current position | Go to Preset 1 |
| Preset 2 | Save current position | Go to Preset 2 |
| Preset 3 | Save current position | Go to Preset 3 |
| Home | - | Return to home position |

**Implementation:** Uses `/usr/local/bin/ptz-preset` binary which sends UVC Extension Unit commands (Unit ID 11, Selector 0x02) directly to the camera firmware. This provides reliable preset recall without drift.

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
sudo mkdir -p /var/www/camviewer /var/www/certbot
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
    'admin': {'hash': bcrypt.hashpw(b'your-password', bcrypt.gensalt(rounds=10)).decode(), 'is_admin': True}
}
with open('/etc/nginx/.wcam-users.json', 'w') as f:
    json.dump(users, f, indent=2)
"
```

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
POST /api/preset/home          # Move camera to home position
POST /api/preset/save/1       # Save current position as Preset 1
POST /api/preset/save/2       # Save current position as Preset 2
POST /api/preset/save/3       # Save current position as Preset 3
POST /api/preset/recall/1     # Go to Preset 1
POST /api/preset/recall/2     # Go to Preset 2
POST /api/preset/recall/3     # Go to Preset 3
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

5. **SSL Certificate**:
   - Certificate stored in `/etc/letsencrypt/live/wagtailcam.gdx.org.uk/`
   - Auto-renews daily via certbot.timer
   - Manual renew: `sudo certbot renew`

6. **Web Files**: `/var/www/camviewer/` (index.html, login.html, auth_server.py, ws_relay.py)

7. **Git**: Code is tracked in git on the Mac at `/Users/gduthie/Programming/Wagtailcam/wanstead/`

---

## References

- [WansteadCam GitHub](https://github.com/grahamduthie/wanstead)
- [uStreamer](https://github.com/pikvm/ustreamer) - MJPEG streamer
- [V4L2 Controls](https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/v4l2.html) - Linux video device API
- [Let's Encrypt](https://letsencrypt.org/) - Free SSL certificates
