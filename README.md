# WansteadCam — Raspberry Pi Webcam Project

A complete, production-ready webcam streaming system running on a Raspberry Pi. Accessible from any browser on the local network or the internet via HTTPS with session-based authentication.

**Live demo:** [wansteadcam.lumoco.com](https://wansteadcam.lumoco.com)

## Features

- **Live MJPEG streaming** from a Logitech QuickCam Pro 5000 via ustreamer (640×480, 30fps)
- **Periodic still images** from a secondary Sweex Mini Webcam via libv4l2 software decoding
- **Cross-browser smooth playback** — WebSocket MJPEG relay prevents stutter on Chrome, Firefox, Safari, and iOS
- **Session-based authentication** with bcrypt-hashed passwords, role-based access (admin/viewer), and a custom login page
- **Admin user management** — add, edit, and delete users via a web GUI modal
- **Event log viewer** — paginated, filterable audit log of all login/logout/user-change events
- **SD card health monitoring** — automatic detection of filesystem issues with self-healing recovery
- **Let's Encrypt TLS** — automatic certificate renewal via certbot
- **fail2ban protection** — bans IPs after repeated failed login attempts
- **Dynamic DNS** — automatic public IP updates via Namecheap
- **Mobile-responsive UI** — works on phones, tablets, and desktops

## Architecture

```
Internet → Port 443 → nginx (TLS + session auth) → webcam viewer
Internet → Port 80  → nginx → 301 redirect → HTTPS
LAN      → Port 8085 → nginx (no auth) → webcam viewer
LAN      → Port 8080 → ustreamer → /dev/video0 (Logitech MJPEG)
Internal → Port 8086 → wcam-auth → Flask/Waitress auth backend
Internal → Port 8087 → wcam-ws-relay → WebSocket MJPEG relay
```

## Hardware

| Component | Detail |
|-----------|--------|
| **Device** | Raspberry Pi 4 (kernel 6.12, aarch64) |
| **Camera 1** | Logitech QuickCam Pro 5000 → `/dev/video0` (UVC, MJPEG, 30fps) |
| **Camera 2** | Sweex Mini Webcam → `/dev/video2` (gspca/sonixb, periodic stills) |
| **Router** | Netgear D7000 — port forwards 80, 443 → Pi |
| **Storage** | 64GB SD card (with health monitoring) |

## Software Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| **nginx** | Reverse proxy | TLS termination, auth gate, rate limiting, WebSocket proxy |
| **wcam-auth** | Python 3 + Flask + Waitress | Session authentication, user CRUD, audit logging |
| **wcam-ws-relay** | Python 3 + asyncio + websockets | MJPEG → WebSocket relay for smooth cross-browser playback |
| **ustreamer** | C | High-performance MJPEG streamer for UVC cameras |
| **sweex-capture** | Bash + v4l2-ctl + ffmpeg | Periodic still capture from gspca-based cameras |
| **fail2ban** | — | SSH and web auth brute-force protection |
| **certbot** | — | Let's Encrypt TLS certificate management |

## Project Structure

```
files/
├── etc_certbot/          # Certbot renewal hooks
├── etc_fail2ban/         # fail2ban jail configs and filters
├── etc_journald/         # systemd journal size limits
├── etc_logrotate/        # Log rotation configs
├── etc_nginx/            # nginx site configs
├── etc_ssh/              # SSH hardening drop-in
├── etc_systemd/          # systemd service units
├── usr_local_bin/        # Custom scripts (DDNS, router reboot, health check)
└── var_www_camviewer/    # Web application (auth backend, relay, HTML)
QWEN.md                   # Detailed engineering log (maintained on the Pi)
```

## Quick Start (on a fresh Pi)

### Prerequisites

```bash
# Install system dependencies
sudo apt install -y nginx python3-pip python3-venv certbot \
  ustreamer v4l-utils ffmpeg fail2ban websockets bcrypt waitress
```

### 1. Deploy configuration files

Copy the `files/` directory structure to the appropriate system locations:

```bash
sudo cp files/etc_nginx/camviewer /etc/nginx/sites-available/camviewer
sudo ln -s /etc/nginx/sites-available/camviewer /etc/nginx/sites-enabled/
sudo cp files/etc_systemd/*.service /etc/systemd/system/
sudo cp files/usr_local_bin/* /usr/local/bin/
sudo chmod +x /usr/local/bin/*.sh /usr/local/bin/*.py
sudo cp files/etc_fail2ban/* /etc/fail2ban/
sudo cp files/etc_ssh/* /etc/ssh/sshd_config.d/
```

### 2. Set up credentials

The following environment variables must be set for scripts that need secrets:

```bash
# Edit root crontab
sudo crontab -e

# Add these lines (replace with your actual values):
*/5 * * * * NAMECHEAP_DDNS_PASSWORD="your-namecheap-ddns-password" /usr/local/bin/namecheap-ddns.sh
0 4 * * 1 ROUTER_IP="192.168.0.1" ROUTER_USER="admin" ROUTER_PASS="your-router-password" /usr/local/bin/reboot-router.py
*/5 * * * * /usr/local/bin/fs-health-check.sh
```

### 3. Create the user database

```bash
# Create initial admin users (replace passwords)
sudo python3 -c "
import bcrypt, json
users = {
    'admin': {'hash': bcrypt.hashpw(b'your-password', bcrypt.gensalt(rounds=10)).decode(), 'is_admin': True}
}
with open('/etc/nginx/.wcam-users.json', 'w') as f:
    json.dump(users, f, indent=2)
"
```

### 4. Set up TLS

```bash
sudo certbot certonly --webroot -w /var/www/certbot -d wansteadcam.lumoco.com
```

### 5. Start services

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nginx wcam-auth wcam-ws-relay ustreamer-logitech ustreamer-sweex fail2ban
```

## SD Card Health & Auto-Recovery

This system is designed to run unattended on an SD card — the most common point of failure on a Pi. Three layers of defence protect against filesystem corruption:

1. **SafeFileHandler** — Custom Python logging handler that falls back to stderr on I/O errors instead of silently dropping log entries
2. **Health check script** — Runs every 5 minutes via cron, detects read-only filesystem, attempts `mount -o remount,rw`, and reboots as a last resort
3. **SD card monitoring** — Checks dmesg for MMC errors, filesystem state, and fsck interval; logs results to the web-accessible Event Log every hour

See `QWEN.md` for the full incident report and technical details.

## Security

- **HTTPS only** — Let's Encrypt TLS, HTTP→HTTPS redirect, HSTS header
- **Session cookies** — httponly, samesite=Lax, secure=True, 24h TTL
- **bcrypt passwords** — `$2y$` hashes, never stored in plaintext
- **fail2ban** — 3 SSH failures or 5 web auth failures → 1 hour ban
- **Rate limiting** — 10 req/s per IP on nginx (burst 20)
- **SSH hardening** — MaxAuthTries 3, LoginGraceTime 30s, X11Forwarding off
- **No secrets in code** — All credentials externalised to environment variables

## API Reference

### Public endpoints

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/api/login` | POST | None | Authenticate, set session cookie |
| `/api/verify` | GET | None (nginx) | Validate session for `auth_request` |
| `/api/health` | GET | None | Health check — returns filesystem + SD card status |

### Authenticated endpoints

| Endpoint | Method | Access | Purpose |
|----------|--------|--------|---------|
| `/api/me` | GET | Any user | Return `{username, is_admin}` |
| `/api/logout` | POST | Any user | Destroy session |
| `/api/users` | GET | Admin | List users (no hashes) |
| `/api/users` | POST | Admin | Create user |
| `/api/users/<name>` | PUT | Admin | Update user |
| `/api/users/<name>` | DELETE | Admin | Delete user (blocked if last admin) |
| `/api/audit` | GET | Admin | Paginated event log |

## License

This project is provided as-is for personal use. Feel free to adapt it for your own webcam setup.

## Acknowledgements

- [ustreamer](https://github.com/pikvm/ustreamer) — excellent MJPEG streamer
- [v4l2-ctl](https://linuxtv.org/) — essential for non-UVC camera support
- [ffmpeg](https://ffmpeg.org/) — image processing for Sweex camera
- [Let's Encrypt](https://letsencrypt.org/) — free TLS certificates
