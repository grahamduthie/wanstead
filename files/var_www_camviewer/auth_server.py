#!/usr/bin/env python3
"""WansteadCam auth backend with user management.
Validates credentials against /etc/nginx/.wcam-users.json (bcrypt),
issues secure session cookies, logs login events for audit,
and provides admin APIs for user management.
Runs under Waitress WSGI server (production-grade).
"""

import bcrypt
import fcntl
import json
import logging
import logging.handlers
import os
import re
import secrets
import sys
import time
from flask import Flask, Response, jsonify, request
from waitress import serve

app = Flask(__name__)

# --- Configuration ---
USERS_FILE = "/etc/nginx/.wcam-users.json"
SESSION_COOKIE_NAME = "wcam_session"
SESSION_TTL = 86400  # 24 hours
SESSION_DIR = "/var/www/camviewer/.sessions"
SESSION_RATE_LIMIT_FILE = "/var/www/camviewer/.rate_limit.json"
RATE_LIMIT_WINDOW = 300  # 5 minutes
RATE_LIMIT_MAX = 10  # max 10 login attempts per window
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{2,32}$")

# --- File-backed session store: sessions stored as individual JSON files ---
# This ensures sessions persist across restarts and work with multiple workers


# --- Safe logging handler: falls back to stderr on any I/O error ---


class SafeFileHandler(logging.handlers.BaseRotatingHandler):
    """A RotatingFileHandler that never crashes the app.

    On any I/O error (read-only filesystem, disk full, etc.):
    1. Writes the log line to stderr as a fallback
    2. Closes the broken file handle
    3. On the next emit(), tries to reopen the file

    This prevents the handler from entering a permanent broken state
    where it retries the same failed operation on every log call.
    """

    def __init__(
        self,
        filename,
        mode="a",
        maxBytes=0,
        backupCount=0,
        when="midnight",
        interval=1,
        encoding="utf-8",
    ):
        # Use TimedRotatingFileHandler logic if when is set, else RotatingFileHandler
        self._use_time_rotation = when is not None
        self._filename = filename
        self._mode = mode
        self._encoding = encoding
        self._maxBytes = maxBytes
        self._backupCount = backupCount
        self._when = when
        self._interval = interval
        self._stream = None
        self._broken = False  # True when file I/O has failed

        logging.Handler.__init__(self)

        if self._use_time_rotation:
            # Initialize TimedRotatingFileHandler state
            self.when = when
            self.interval = interval
            self.suffix = "%Y-%m-%d"
            self.extMatch = r"^\d{4}-\d{2}-\d{2}(\.\w+)?$"
            self.baseFilename = filename
            self._compute_fn_prefix_suffix()
            self.rolloverAt = self._compute_rollover()
        else:
            self.baseFilename = filename
            self.maxBytes = maxBytes
            self.backupCount = backupCount

        # Open the file now
        self._open_file()

    def _compute_fn_prefix_suffix(self):
        """Compute the prefix and suffix for timed rotation filenames."""
        import re as re_mod

        self.extMatch = re_mod.compile(r"^\d{4}-\d{2}-\d{2}(\.\w+)?$")
        # Compute suffix from current time
        self.suffix = time.strftime("%Y-%m-%d")
        self._fn_prefix = self.baseFilename + "."

    def _compute_rollover(self):
        """Compute the next rollover time as an epoch timestamp."""
        import datetime

        if self.when == "midnight":
            # Compute next midnight in epoch seconds
            now = datetime.datetime.now()
            tomorrow = now.replace(
                hour=0, minute=0, second=0, microsecond=0
            ) + datetime.timedelta(days=1)
            return int(tomorrow.timestamp())
        # Fallback: current time + interval seconds
        return int(time.time()) + self.interval

    def _open_file(self):
        """Open the log file. Sets self._broken on failure."""
        try:
            if self._stream and not self._stream.closed:
                self._stream.close()
            self._stream = open(self.baseFilename, self._mode, encoding=self._encoding)
            self._broken = False
        except OSError:
            self._broken = True
            self._stream = None
            print(f"SAFE_HANDLER_BROKEN: cannot open {self._filename}", file=sys.stderr)

    def shouldRollover(self, record):
        """Determine if rollover should occur."""
        if self._broken:
            return False
        if self._use_time_rotation:
            t = int(time.time())
            if t >= self.rolloverAt:
                return 1
        else:
            if self._stream is None:
                return False
            self._stream.seek(0, 2)  # due to non-posix-compliant while clause
            if self._stream.tell() + len(self.format(record)) >= self._maxBytes:
                return 1
        return 0

    def doRollover(self):
        """Perform the rollover."""
        if self._use_time_rotation:
            # TimedRotatingFileHandler-style rotation
            dfn = self._fn_prefix + self.suffix
            try:
                if os.path.exists(dfn):
                    os.remove(dfn)
                if os.path.exists(self.baseFilename):
                    os.rename(self.baseFilename, dfn)
                # Update rolloverAt for next midnight
                self.rolloverAt = self._compute_rollover()
                self.suffix = time.strftime("%Y-%m-%d")
            except OSError as e:
                print(f"SAFE_HANDLER_ROLLOVER_FAIL: {e}", file=sys.stderr)
                self._broken = True
                if self._stream and not self._stream.closed:
                    self._stream.close()
                self._stream = None
                return
        else:
            # RotatingFileHandler-style rotation
            if self.backupCount > 0:
                for i in range(self.backupCount - 1, 0, -1):
                    sfn = f"{self.baseFilename}.{i}"
                    dfn = f"{self.baseFilename}.{i + 1}"
                    if os.path.exists(sfn):
                        if os.path.exists(dfn):
                            os.remove(dfn)
                        os.rename(sfn, dfn)
                dfn = self.baseFilename + ".1"
                if os.path.exists(self.baseFilename):
                    os.rename(self.baseFilename, dfn)

        # Reopen the base file
        self._open_file()

    def emit(self, record):
        """Emit a record, falling back to stderr on any I/O error."""
        try:
            if self._broken:
                # Try to recover: reopen the file
                self._open_file()
                if self._broken:
                    # Still broken, fall through to stderr
                    raise OSError(f"Cannot open {self._filename}")

            if self.shouldRollover(record):
                self.doRollover()
                if self._broken:
                    raise OSError(f"Rollover failed for {self._filename}")

            if self._stream is None:
                raise OSError(f"No stream for {self._filename}")

            msg = self.format(record)
            stream = self._stream
            stream.write(msg + self.terminator)
            stream.flush()
        except Exception:
            self._broken = True
            if self._stream and not self._stream.closed:
                self._stream.close()
            self._stream = None
            # Fallback to stderr so we never lose the log record
            print(f"LOG_FALLBACK: {self.format(record)}", file=sys.stderr)


# --- Logging: auth events (fail2ban) ---
AUTH_LOG_PATH = "/var/log/wcam-auth.log"
auth_log_handler = SafeFileHandler(
    AUTH_LOG_PATH,
    maxBytes=1_000_000,
    backupCount=5,
    when=None,
    interval=1,
    encoding="utf-8",
)
auth_log_handler.setFormatter(
    logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
auth_log_handler.setLevel(logging.INFO)

auth_logger = logging.getLogger("wcam-auth")
auth_logger.addHandler(auth_log_handler)
auth_logger.setLevel(logging.INFO)

# --- Logging: login audit log (1 year retention) ---
AUDIT_LOG_PATH = "/var/log/wcam-login.log"
audit_log_handler = SafeFileHandler(
    AUDIT_LOG_PATH, when="midnight", interval=1, backupCount=365, encoding="utf-8"
)
audit_log_handler.setFormatter(
    logging.Formatter("%(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
audit_log_handler.setLevel(logging.INFO)

audit_logger = logging.getLogger("wcam-audit")
audit_logger.addHandler(audit_log_handler)
audit_logger.setLevel(logging.INFO)

# Silence Flask/Werkzeug request logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)


# --- Helpers ---


def get_client_ip():
    """Get real client IP from X-Real-IP header (set by nginx) or fallback."""
    return request.headers.get("X-Real-IP", request.remote_addr)


def load_users():
    """Load user database from JSON file. Returns {username: {hash, is_admin}}."""
    try:
        with open(USERS_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = json.load(f)
            fcntl.flock(f, fcntl.LOCK_UN)
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_users(users):
    """Atomically write user database to JSON file.
    Returns True on success, False on failure (logged to auth log).
    """
    tmp_path = USERS_FILE + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(users, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
            fcntl.flock(f, fcntl.LOCK_UN)
        os.replace(tmp_path, USERS_FILE)
        return True
    except OSError as e:
        auth_logger.error("SAVE_USERS_FAILED: %s — user changes lost", e)
        print(f"SAVE_USERS_FAILED: {e}", file=sys.stderr)
        # Clean up temp file if it was created
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return False


def verify_password(username, password):
    """Check password against bcrypt hash in user database."""
    users = load_users()
    user = users.get(username)
    if not user:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), user["hash"].encode("utf-8"))
    except Exception:
        return False


def _get_session_path(token):
    """Get the file path for a session token."""
    return os.path.join(SESSION_DIR, f"session_{token[:2]}", f"{token}.json")


def _ensure_session_dir():
    """Ensure the session directory structure exists."""
    subdir = os.path.join(SESSION_DIR, "session_00")
    os.makedirs(subdir, exist_ok=True)


def create_session(username, is_admin):
    """Create a new session and return the token."""
    _ensure_session_dir()
    token = secrets.token_urlsafe(32)
    session_data = {
        "username": username,
        "is_admin": is_admin,
        "expires": time.time() + SESSION_TTL,
    }
    session_path = _get_session_path(token)
    os.makedirs(os.path.dirname(session_path), exist_ok=True)
    try:
        with open(session_path, "w") as f:
            json.dump(session_data, f)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        auth_logger.error("SESSION_CREATE_FAILED: %s — %s", token[:8], e)
        return None
    return token


def verify_session(token):
    """Verify a session token. Returns {username, is_admin} or None."""
    if not token:
        return None
    session_path = _get_session_path(token)
    try:
        with open(session_path, "r") as f:
            session = json.load(f)
        if session["expires"] < time.time():
            os.unlink(session_path)
            return None
        return {"username": session["username"], "is_admin": session["is_admin"]}
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None
    except OSError as e:
        auth_logger.warning("SESSION_READ_ERROR: %s — %s", token[:8], e)
        return None


def check_rate_limit(ip):
    """Check if IP is rate limited. Returns (allowed, remaining_attempts)."""
    try:
        if os.path.exists(SESSION_RATE_LIMIT_FILE):
            with open(SESSION_RATE_LIMIT_FILE, "r") as f:
                data = json.load(f)
        else:
            data = {"attempts": {}}
    except (json.JSONDecodeError, OSError):
        data = {"attempts": {}}

    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW

    data["attempts"] = {
        k: [t for t in v if t > window_start] for k, v in data["attempts"].items()
    }

    ip_attempts = data["attempts"].get(ip, [])
    remaining = RATE_LIMIT_MAX - len(ip_attempts)

    if remaining <= 0:
        return False, 0

    return True, remaining


def record_failed_attempt(ip):
    """Record a failed login attempt for rate limiting."""
    try:
        if os.path.exists(SESSION_RATE_LIMIT_FILE):
            with open(SESSION_RATE_LIMIT_FILE, "r") as f:
                data = json.load(f)
        else:
            data = {"attempts": {}}
    except (json.JSONDecodeError, OSError):
        data = {"attempts": {}}

    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW

    if ip not in data["attempts"]:
        data["attempts"][ip] = []
    data["attempts"][ip] = [t for t in data["attempts"][ip] if t > window_start]
    data["attempts"][ip].append(now)

    try:
        with open(SESSION_RATE_LIMIT_FILE, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        auth_logger.warning("RATE_LIMIT_WRITE_ERROR: %s", e)


def clear_rate_limit(ip):
    """Clear rate limit for IP after successful login."""
    try:
        if os.path.exists(SESSION_RATE_LIMIT_FILE):
            with open(SESSION_RATE_LIMIT_FILE, "r") as f:
                data = json.load(f)
        else:
            return
    except (json.JSONDecodeError, OSError):
        return

    if ip in data["attempts"]:
        del data["attempts"][ip]
        try:
            with open(SESSION_RATE_LIMIT_FILE, "w") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            pass


def delete_session(token):
    """Delete a session file."""
    if not token:
        return
    session_path = _get_session_path(token)
    try:
        os.unlink(session_path)
    except FileNotFoundError:
        pass
    except OSError as e:
        auth_logger.warning("SESSION_DELETE_ERROR: %s — %s", token[:8], e)


def clean_expired_sessions():
    """Remove expired session files. Called periodically."""
    if not os.path.isdir(SESSION_DIR):
        return
    now = time.time()
    for subdir in os.listdir(SESSION_DIR):
        subdir_path = os.path.join(SESSION_DIR, subdir)
        if not os.path.isdir(subdir_path):
            continue
        for filename in os.listdir(subdir_path):
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(subdir_path, filename)
            try:
                with open(filepath, "r") as f:
                    session = json.load(f)
                if session.get("expires", 0) < now:
                    os.unlink(filepath)
            except (FileNotFoundError, json.JSONDecodeError, KeyError):
                try:
                    os.unlink(filepath)
                except OSError:
                    pass
            except OSError:
                pass


def require_admin():
    """Check if the current request has a valid admin session. Returns user info or None."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    info = verify_session(token)
    if info and info["is_admin"]:
        return info
    return None


def audit_log(event_type, username, ip, detail=""):
    """Write a structured JSON line to the audit log.
    The SafeFileHandler already falls back to stderr on I/O errors,
    but we add a secondary fallback to auth_log for extra safety.
    """
    entry = json.dumps(
        {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event": event_type,
            "username": username,
            "ip": ip,
            "detail": detail,
        }
    )
    try:
        audit_logger.info(entry)
    except Exception:
        # Double safety: if SafeFileHandler's fallback itself fails
        print(f"AUDIT_LOG_FALLBACK: {entry}", file=sys.stderr)
        try:
            auth_logger.error("AUDIT_LOG: %s", entry)
        except Exception:
            pass  # Truly nowhere to write — stderr already got it


def check_filesystem_writable():
    """Check if the filesystem is writable. Returns (ok, error_string)."""
    test_path = "/var/log/.fs_write_test"
    try:
        with open(test_path, "w") as f:
            f.write("1")
        os.unlink(test_path)
        return True, None
    except OSError as e:
        return False, str(e)


def get_sd_card_health():
    """Check SD card health indicators.
    Returns {status, details} where status is 'ok', 'warning', or 'critical'.
    """
    import subprocess

    issues = []
    status = "ok"

    # Check for mmc0 errors in dmesg
    try:
        result = subprocess.run(
            ["dmesg", "-T"], capture_output=True, text=True, timeout=5
        )
        dmesg_output = result.stdout
        mmc_errors = len(
            [
                l
                for l in dmesg_output.splitlines()
                if "mmc0" in l
                and any(k in l for k in ["error", "timeout", "reset", "CRC"])
            ]
        )
        if mmc_errors > 0:
            issues.append(f"{mmc_errors} mmc0 errors in dmesg")
            status = "warning"

        io_errors = len(
            [
                l
                for l in dmesg_output.splitlines()
                if any(
                    k in l
                    for k in ["I/O error", "blk_update_request", "Buffer I/O error"]
                )
            ]
        )
        if io_errors > 0:
            issues.append(f"{io_errors} I/O errors in dmesg")
            status = "critical"
    except Exception as e:
        auth_logger.debug("DMESG_CHECK_SKIP: %s", e)

    # Check filesystem state
    try:
        result = subprocess.run(
            ["tune2fs", "-l", "/dev/mmcblk0p2"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.splitlines():
            if line.startswith("Filesystem state:"):
                fs_state = line.split(":")[1].strip()
                if fs_state != "clean":
                    issues.append(f"Filesystem state: {fs_state}")
                    status = "critical"
            if line.startswith("Mount count:"):
                mount_count = int(line.split(":")[1].strip())
                issues.append(f"Mount count: {mount_count}")
            if line.startswith("Last checked:"):
                last_checked = line.split(":", 1)[1].strip()
                issues.append(f"Last checked: {last_checked}")
    except Exception as e:
        auth_logger.debug("TUNE2FS_CHECK_SKIP: %s", e)

    # Check for reboot recovery marker
    reboot_marker = "/var/log/.fs_recovery_reboot_pending"
    if os.path.exists(reboot_marker):
        age = time.time() - os.path.getmtime(reboot_marker)
        issues.append(f"Recovery reboot pending (marker age: {int(age)}s)")
        if status != "critical":
            status = "warning"

    return {"status": status, "issues": issues if issues else ["healthy"]}


# --- SD card health logging: log status periodically to audit log ---

_sd_health_last_logged = 0
_SD_HEALTH_INTERVAL = 3600  # Log SD card health to audit log every hour


def log_sd_card_health_if_due():
    """Log SD card health status to audit log periodically.
    Only logs when status is not 'ok', or once per hour for 'ok'.
    Called on each request but throttled by interval.
    """
    global _sd_health_last_logged
    now = time.time()
    if now - _sd_health_last_logged < _SD_HEALTH_INTERVAL:
        return
    _sd_health_last_logged = now

    health = get_sd_card_health()
    if health["status"] == "ok":
        audit_log("SD_CARD_HEALTH", "system", "127.0.0.1", "status=ok")
    else:
        detail = f"status={health['status']} issues={'; '.join(health['issues'])}"
        audit_log("SD_CARD_HEALTH", "system", "127.0.0.1", detail)


# --- Health check endpoint ---


@app.route("/api/health", methods=["GET"])
def api_health():
    """Health check for monitoring. Returns filesystem, SD card, and service status.
    No auth required — used by external monitoring and the health check cron job.
    Also logs SD card health to audit log periodically (visible in webGUI).
    """
    fs_ok, fs_err = check_filesystem_writable()
    sd_health = get_sd_card_health()

    # Log SD card health to audit log periodically (visible in webGUI log viewer)
    try:
        log_sd_card_health_if_due()
    except Exception as e:
        auth_logger.debug("SD_HEALTH_LOG_SKIP: %s", e)

    status = {
        "ok": fs_ok and sd_health["status"] != "critical",
        "filesystem_writable": fs_ok,
        "filesystem_error": fs_err,
        "sd_card": sd_health,
        "uptime": time.time(),
    }
    http_code = 200 if status["ok"] else 503
    return jsonify(status), http_code


# --- Public API endpoints ---


@app.route("/api/login", methods=["POST"])
def api_login():
    client_ip = get_client_ip()

    allowed, remaining = check_rate_limit(client_ip)
    if not allowed:
        auth_logger.warning("RATE_LIMITED ip=%s", client_ip)
        audit_log("RATE_LIMITED", "unknown", client_ip, "too_many_attempts")
        return jsonify(
            {"ok": False, "error": "Too many login attempts. Try again later."}
        ), 429

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": False, "error": "Invalid request"}), 400

    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username or not password:
        return jsonify({"ok": False, "error": "Username and password required"}), 400

    users = load_users()
    user = users.get(username)
    if user and verify_password(username, password):
        clear_rate_limit(client_ip)
        token = create_session(username, user.get("is_admin", False))
        if not token:
            return jsonify({"ok": False, "error": "Session creation failed"}), 500
        auth_logger.info("LOGIN_OK user=%s ip=%s", username, client_ip)
        audit_log("LOGIN_OK", username, client_ip)
        resp = jsonify({"ok": True})
        resp.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            httponly=True,
            samesite="Lax",
            secure=True,
            max_age=SESSION_TTL,
            path="/",
        )
        return resp
    else:
        record_failed_attempt(client_ip)
        auth_logger.warning("LOGIN_FAIL user=%s ip=%s", username, client_ip)
        audit_log("LOGIN_FAIL", username, client_ip, "invalid_credentials")
        return jsonify({"ok": False, "error": "Invalid username or password"}), 401


@app.route("/api/verify", methods=["GET"])
def api_verify():
    """Check if the session cookie is valid. Used by nginx auth_request."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    info = verify_session(token)
    if info:
        resp = Response(status=200)
        resp.headers["X-Auth-User"] = info["username"]
        resp.headers["X-Auth-Admin"] = "1" if info["is_admin"] else "0"
        return resp
    return Response(status=401)


@app.route("/api/me", methods=["GET"])
def api_me():
    """Return current user info. Used by frontend to determine admin UI visibility."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    info = verify_session(token)
    if info:
        return jsonify(
            {"ok": True, "username": info["username"], "is_admin": info["is_admin"]}
        )
    return jsonify({"ok": False}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    token = request.cookies.get(SESSION_COOKIE_NAME)
    info = verify_session(token)
    if info:
        audit_log("LOGOUT", info["username"], get_client_ip())
    delete_session(token)
    resp = jsonify({"ok": True})
    resp.set_cookie(SESSION_COOKIE_NAME, "", expires=0, path="/")
    return resp


# --- Admin-only user management endpoints ---


@app.route("/api/users", methods=["GET"])
def api_list_users():
    """List all users (admin only). Returns usernames and admin flags, never hashes."""
    admin = require_admin()
    if not admin:
        return jsonify({"ok": False, "error": "Admin access required"}), 403

    users = load_users()
    result = []
    for uname, udata in sorted(users.items()):
        result.append({"username": uname, "is_admin": udata.get("is_admin", False)})
    return jsonify({"ok": True, "users": result})


@app.route("/api/users", methods=["POST"])
def api_create_user():
    """Create a new user (admin only). Requires username, password, is_admin."""
    admin = require_admin()
    if not admin:
        return jsonify({"ok": False, "error": "Admin access required"}), 403

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": False, "error": "Invalid request"}), 400

    username = data.get("username", "").strip()
    password = data.get("password", "")
    is_admin = bool(data.get("is_admin", False))

    # Validate username format
    if not USERNAME_RE.match(username):
        return jsonify(
            {
                "ok": False,
                "error": "Username must be 2-32 chars, alphanumeric/underscore/hyphen/dot only",
            }
        ), 400

    # Validate password
    if len(password) < 4:
        return jsonify(
            {"ok": False, "error": "Password must be at least 4 characters"}
        ), 400

    users = load_users()
    if username in users:
        return jsonify({"ok": False, "error": "Username already exists"}), 409

    # Hash password with bcrypt (12 rounds per OWASP 2026 recommendation)
    password_hash = bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=12)
    ).decode("utf-8")
    users[username] = {"hash": password_hash, "is_admin": is_admin}
    if not save_users(users):
        return jsonify(
            {"ok": False, "error": "Failed to save user — filesystem error"}
        ), 500

    audit_log(
        "USER_CREATED",
        admin["username"],
        get_client_ip(),
        f"user={username} admin={is_admin}",
    )
    auth_logger.info(
        "USER_CREATED by=%s user=%s admin=%s", admin["username"], username, is_admin
    )

    return jsonify({"ok": True, "user": {"username": username, "is_admin": is_admin}})


@app.route("/api/users/<target_username>", methods=["PUT"])
def api_update_user(target_username):
    """Update an existing user (admin only). Can change username, password, is_admin."""
    admin = require_admin()
    if not admin:
        return jsonify({"ok": False, "error": "Admin access required"}), 403

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": False, "error": "Invalid request"}), 400

    users = load_users()
    if target_username not in users:
        return jsonify({"ok": False, "error": "User not found"}), 404

    new_username = data.get("username", target_username).strip()
    new_password = data.get("password", "")
    new_is_admin = data.get("is_admin", users[target_username].get("is_admin", False))

    # Validate new username if changing
    if new_username != target_username:
        if not USERNAME_RE.match(new_username):
            return jsonify(
                {
                    "ok": False,
                    "error": "Username must be 2-32 chars, alphanumeric/underscore/hyphen/dot only",
                }
            ), 400
        if new_username in users:
            return jsonify({"ok": False, "error": "Username already exists"}), 409

    # Validate password if changing
    if new_password and len(new_password) < 4:
        return jsonify(
            {"ok": False, "error": "Password must be at least 4 characters"}
        ), 400

    # Build updated user record
    user_record = dict(users[target_username])
    if new_password:
        user_record["hash"] = bcrypt.hashpw(
            new_password.encode("utf-8"), bcrypt.gensalt(rounds=12)
        ).decode("utf-8")
    user_record["is_admin"] = new_is_admin

    # If username is changing, delete old key and add new key
    if new_username != target_username:
        del users[target_username]
        users[new_username] = user_record
    else:
        users[target_username] = user_record

    if not save_users(users):
        return jsonify(
            {"ok": False, "error": "Failed to save user — filesystem error"}
        ), 500

    changes = []
    if new_username != target_username:
        changes.append(f"renamed {target_username}->{new_username}")
    if new_password:
        changes.append("password_changed")
    if "is_admin" in data:
        changes.append(f"admin={new_is_admin}")

    audit_log(
        "USER_UPDATED",
        admin["username"],
        get_client_ip(),
        f"target={new_username} changes={', '.join(changes)}",
    )
    auth_logger.info(
        "USER_UPDATED by=%s target=%s changes=%s",
        admin["username"],
        new_username,
        ", ".join(changes),
    )

    return jsonify(
        {"ok": True, "user": {"username": new_username, "is_admin": new_is_admin}}
    )


@app.route("/api/users/<target_username>", methods=["DELETE"])
def api_delete_user(target_username):
    """Delete a user (admin only). Cannot delete the last admin user."""
    admin = require_admin()
    if not admin:
        return jsonify({"ok": False, "error": "Admin access required"}), 403

    users = load_users()
    if target_username not in users:
        return jsonify({"ok": False, "error": "User not found"}), 404

    # Prevent deleting the last admin
    admin_count = sum(1 for u in users.values() if u.get("is_admin", False))
    target_is_admin = users[target_username].get("is_admin", False)
    if target_is_admin and admin_count <= 1:
        return jsonify({"ok": False, "error": "Cannot delete the last admin user"}), 400

    del users[target_username]
    if not save_users(users):
        return jsonify(
            {"ok": False, "error": "Failed to delete user — filesystem error"}
        ), 500

    audit_log(
        "USER_DELETED",
        admin["username"],
        get_client_ip(),
        f"deleted_user={target_username}",
    )
    auth_logger.info("USER_DELETED by=%s target=%s", admin["username"], target_username)

    return jsonify({"ok": True})


@app.route("/api/audit", methods=["GET"])
def api_audit_log():
    """Return recent entries from the login audit log (admin only).
    Query params: page (default 1), per_page (default 50, max 200),
    event (optional filter), username (optional filter).
    Always reads rotated files so the most recent 50 entries are visible.
    """
    admin = require_admin()
    if not admin:
        return jsonify({"ok": False, "error": "Admin access required"}), 403

    page = max(1, int(request.args.get("page", 1)))
    per_page = min(200, max(10, int(request.args.get("per_page", 50))))
    event_filter = request.args.get("event", "").strip()
    username_filter = request.args.get("username", "").strip()
    has_filters = bool(event_filter or username_filter)

    import glob as glob_mod

    # Read all log files (current + rotated), sort by timestamp descending
    entries = []
    log_files = sorted(glob_mod.glob(AUDIT_LOG_PATH + "*"))
    if AUDIT_LOG_PATH in log_files:
        log_files.remove(AUDIT_LOG_PATH)
        log_files.insert(0, AUDIT_LOG_PATH)

    for log_file in log_files:
        try:
            with open(log_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if event_filter and entry.get("event") != event_filter:
                            continue
                        if username_filter and entry.get("username") != username_filter:
                            continue
                        entries.append(entry)
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            continue

    # Sort newest first by timestamp
    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)

    total = len(entries)
    start = (page - 1) * per_page
    end = start + per_page
    page_entries = entries[start:end]

    return jsonify(
        {
            "ok": True,
            "entries": page_entries,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page if per_page else 1,
        }
    )


# --- PTZ Control endpoint ---


@app.route("/api/ptz", methods=["GET"])
def api_ptz():
    """Control PTZ camera via v4l2-ctl.
    Query params: pan (int -1/0/1), tilt (int -1/0/1), zoom (int 100-1000), focus (int 0-255),
                  focus_auto (0/1 to enable/disable auto focus).
    No auth required (already behind nginx auth).
    """
    import subprocess

    device = "/dev/video0"
    cmd = ["v4l2-ctl", "-d", device]

    pan = request.args.get("pan")
    tilt = request.args.get("tilt")
    zoom = request.args.get("zoom")
    focus = request.args.get("focus")
    focus_auto = request.args.get("focus_auto")

    try:
        if pan is not None or tilt is not None:
            pan_val = int(pan) if pan is not None else 0
            tilt_val = int(tilt) if tilt is not None else 0
            subprocess.run(
                cmd + ["-c", f"pan_speed={pan_val}", "-c", f"tilt_speed={tilt_val}"],
                capture_output=True,
                timeout=2,
            )

        if zoom is not None:
            zoom_val = max(100, min(1000, int(zoom)))
            subprocess.run(
                cmd + ["-c", f"zoom_absolute={zoom_val}"],
                capture_output=True,
                timeout=2,
            )

        if focus is not None:
            focus_val = max(0, min(255, int(focus)))
            subprocess.run(
                cmd + ["-c", f"focus_absolute={focus_val}"],
                capture_output=True,
                timeout=2,
            )

        if focus_auto is not None:
            auto_val = 1 if focus_auto in ("1", "true", "True") else 0
            subprocess.run(
                cmd + ["-c", f"focus_automatic_continuous={auto_val}"],
                capture_output=True,
                timeout=2,
            )

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/ptz/state", methods=["GET"])
def api_ptz_state():
    """Get current PTZ camera state from hardware."""
    import subprocess

    device = "/dev/video0"
    result = {"zoom": None, "focus": None, "focus_auto": None}

    try:
        proc = subprocess.run(
            [
                "v4l2-ctl",
                "-d",
                device,
                "-C",
                "zoom_absolute",
                "-C",
                "focus_absolute",
                "-C",
                "focus_automatic_continuous",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
        for line in proc.stdout.strip().split("\n"):
            if ":" in line:
                key, val = line.split(":", 1)
                key = key.strip()
                val = val.strip()
                if key == "zoom_absolute":
                    result["zoom"] = int(val)
                elif key == "focus_absolute":
                    result["focus"] = int(val)
                elif key == "focus_automatic_continuous":
                    result["focus_auto"] = val == "1"

        return jsonify({"ok": True, "state": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


PRESET_BIN = "/usr/local/bin/ptz-preset"


@app.route("/api/preset/save/<int:preset_num>", methods=["POST"])
def api_preset_save(preset_num):
    """Save current position as a preset (1-3)."""
    import subprocess

    if preset_num not in (1, 2, 3):
        return jsonify({"ok": False, "error": "Preset must be 1, 2, or 3"}), 400
    try:
        result = subprocess.run(
            [PRESET_BIN, f"save{preset_num}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return jsonify({"ok": True})
        else:
            return jsonify({"ok": False, "error": result.stderr}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/preset/recall/<int:preset_num>", methods=["POST"])
def api_preset_recall(preset_num):
    """Move camera to a saved preset (1-3)."""
    import subprocess

    if preset_num not in (1, 2, 3):
        return jsonify({"ok": False, "error": "Preset must be 1, 2, or 3"}), 400
    try:
        result = subprocess.run(
            [PRESET_BIN, f"preset{preset_num}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return jsonify({"ok": True})
        else:
            return jsonify({"ok": False, "error": result.stderr}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/preset/home", methods=["POST"])
def api_preset_home():
    """Move camera to home position using UVC extension unit."""
    import subprocess

    try:
        result = subprocess.run(
            [PRESET_BIN, "home"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return jsonify({"ok": True})
        else:
            return jsonify({"ok": False, "error": result.stderr}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


PRESET_NAMES_FILE = "/var/www/camviewer/.preset_names.json"


def load_preset_names():
    """Load preset names and focus values from JSON file."""
    import json

    try:
        with open(PRESET_NAMES_FILE, "r") as f:
            data = json.load(f)
            for i in ["1", "2", "3"]:
                if i not in data:
                    data[i] = {"name": "", "focus": None}
                elif isinstance(data[i], str):
                    data[i] = {"name": data[i], "focus": None}
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "1": {"name": "", "focus": None},
            "2": {"name": "", "focus": None},
            "3": {"name": "", "focus": None},
        }


def save_preset_names(names):
    """Save preset names and focus values to JSON file."""
    import json

    with open(PRESET_NAMES_FILE, "w") as f:
        json.dump(names, f)


@app.route("/api/preset/names", methods=["GET"])
def api_preset_names():
    """Get all preset names and focus values."""
    names = load_preset_names()
    return jsonify({"ok": True, "presets": names})


@app.route("/api/preset/<int:preset_num>", methods=["PUT"])
def api_preset_update(preset_num):
    """Update preset name and/or focus value."""
    if preset_num not in (1, 2, 3):
        return jsonify({"ok": False, "error": "Preset must be 1, 2, or 3"}), 400

    try:
        data = request.get_json()
        presets = load_preset_names()
        key = str(preset_num)
        if "name" in data:
            presets[key]["name"] = data["name"][:50]
        if "focus" in data:
            focus = data["focus"]
            if focus is None or (isinstance(focus, int) and 0 <= focus <= 255):
                presets[key]["focus"] = focus
        save_preset_names(presets)
        return jsonify({"ok": True, "preset": presets[key]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/preset/<int:preset_num>/focus", methods=["GET"])
def api_preset_focus(preset_num):
    """Get the focus value for a preset."""
    if preset_num not in (1, 2, 3):
        return jsonify({"ok": False, "error": "Preset must be 1, 2, or 3"}), 400
    presets = load_preset_names()
    return jsonify({"ok": True, "focus": presets[str(preset_num)]["focus"]})


TIMELAPSE_DIR = "/mnt/nas/timelapse"
CAPTURE_STATUS_FILE = "/var/www/camviewer/.capture_status.json"


@app.route("/api/timelapse/dates", methods=["GET"])
def api_timelapse_dates():
    """Get list of dates that have timelapse images."""
    import os
    import glob

    dates = []
    if os.path.isdir(TIMELAPSE_DIR):
        for d in sorted(os.listdir(TIMELAPSE_DIR), reverse=True):
            date_path = os.path.join(TIMELAPSE_DIR, d)
            if os.path.isdir(date_path):
                images = [f for f in os.listdir(date_path) if f.endswith(".jpg")]
                if images:
                    dates.append({"date": d, "count": len(images)})
    return jsonify({"ok": True, "dates": dates})


@app.route("/api/capture/status", methods=["GET"])
def api_capture_status():
    """Get current capture status for browser notifications."""
    import json

    try:
        with open(CAPTURE_STATUS_FILE, "r") as f:
            status = json.load(f)
        return jsonify({"ok": True, "status": status})
    except (FileNotFoundError, json.JSONDecodeError):
        return jsonify({"ok": True, "status": {"capturing": False, "message": ""}})


@app.route("/api/capture/status", methods=["POST"])
def api_capture_status_set():
    """Set capture status (called by capture script)."""
    import json

    try:
        data = request.get_json()
        with open(CAPTURE_STATUS_FILE, "w") as f:
            json.dump(data, f)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def require_login(f):
    """Decorator to require valid session."""
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get(SESSION_COOKIE_NAME)
        info = verify_session(token)
        if not info:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)

    return decorated


@app.route("/api/timelapse/list", methods=["GET"])
@require_login
def api_timelapse_list():
    """List all images for a given date."""
    import os

    date_str = request.args.get("date")
    if not date_str:
        return jsonify({"ok": False, "error": "Date required"}), 400

    date_dir = os.path.join(TIMELAPSE_DIR, date_str)
    if not os.path.isdir(date_dir):
        return jsonify({"ok": True, "images": []})

    images = []
    for f in sorted(os.listdir(date_dir)):
        if f.endswith(".jpg"):
            images.append(f"/timelapse/{date_str}/{f}")
    return jsonify({"ok": True, "images": images})


@app.route("/api/timelapse/image", methods=["GET"])
@require_login
def api_timelapse_image():
    """Serve a timelapse image."""
    import os

    path = request.args.get("path")
    if not path:
        return "Path required", 400

    safe_path = os.path.normpath(path)
    if ".." in safe_path or not safe_path.startswith("/timelapse/"):
        return "Invalid path", 400

    real_path = os.path.join("/mnt/nas", safe_path.lstrip("/"))
    if not os.path.isfile(real_path):
        return "Not found", 404

    from flask import send_file

    return send_file(real_path, mimetype="image/jpeg")


@app.route("/api/timelapse/download", methods=["GET"])
@require_login
def api_timelapse_download():
    """Download all images for a date as a zip file."""
    import os
    import io
    import zipfile
    from flask import make_response

    date_str = request.args.get("date")
    if not date_str:
        return "Date required", 400

    date_dir = os.path.join(TIMELAPSE_DIR, date_str)
    if not os.path.isdir(date_dir):
        return "No images for this date", 404

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(os.listdir(date_dir)):
            if f.endswith(".jpg"):
                file_path = os.path.join(date_dir, f)
                zf.write(file_path, f)

    memory_file.seek(0)
    response = make_response(memory_file.getvalue())
    response.headers["Content-Type"] = "application/zip"
    response.headers["Content-Disposition"] = (
        f"attachment; filename=timelapse_{date_str}.zip"
    )
    return response


@app.route("/api/timelapse/stream", methods=["GET"])
@require_login
def api_timelapse_stream():
    """Stream timelapse images as MJPEG multipart stream."""
    import os
    import time

    date_str = request.args.get("date")
    if not date_str:
        return "Date required", 400

    date_dir = os.path.join(TIMELAPSE_DIR, date_str)
    if not os.path.isdir(date_dir):
        return "No images for this date", 404

    try:
        speed = int(request.args.get("speed", "300"))
    except ValueError:
        speed = 300
    speed = max(50, min(5000, speed))

    image_files = sorted([f for f in os.listdir(date_dir) if f.endswith(".jpg")])

    if not image_files:
        return "No images for this date", 404

    BOUNDARY = "boundarydonotcross"

    def generate():
        for img_file in image_files:
            file_path = os.path.join(date_dir, img_file)
            try:
                with open(file_path, "rb") as f:
                    jpeg_data = f.read()
            except OSError:
                continue

            if len(jpeg_data) < 1000 or not jpeg_data.startswith(b"\xff\xd8"):
                continue

            yield f"--{BOUNDARY}\r\n".encode()
            yield b"Content-Type: image/jpeg\r\n"
            yield f"Content-Length: {len(jpeg_data)}\r\n".encode()
            yield b"\r\n"
            yield jpeg_data
            yield b"\r\n"

            time.sleep(speed / 1000.0)

    from flask import Response

    response = Response(
        generate(), mimetype=f"multipart/x-mixed-replace; boundary={BOUNDARY}"
    )
    return response


@app.route("/api/time", methods=["GET"])
def api_time():
    """Return the Pi's current Unix timestamp and IANA timezone so the browser
    can display the correct local time regardless of where the viewer is."""
    tz = "Europe/London"
    try:
        with open("/etc/timezone") as f:
            tz = f.read().strip()
    except OSError:
        pass
    return jsonify({"ok": True, "epoch": time.time(), "timezone": tz})


# ---------------------------------------------------------------------------
# Timelapse video generation
# ---------------------------------------------------------------------------
import subprocess
import tempfile
import threading

_video_gen_lock = threading.Lock()
_video_gen_active = set()  # date strings currently being generated


def _video_needs_regen(date_dir, video_path):
    """Return True if the MP4 doesn't exist or a newer JPEG has been captured since.

    A freshness grace period of 5 minutes is applied: if the video was generated
    recently, we consider it up-to-date even if one or two new frames have arrived.
    This prevents a continuous regen loop while timelapse capture is in progress
    (frames every 5 min, generation takes ~30 s).
    """
    _FRESHNESS_SECS = 300  # 5 minutes — matches the capture interval
    try:
        if not os.path.isfile(video_path):
            return True
        video_mtime = os.path.getmtime(video_path)
        # If the video is fresh enough, don't trigger another generation yet
        if (time.time() - video_mtime) < _FRESHNESS_SECS:
            return False
        for f in os.listdir(date_dir):
            if f.endswith(".jpg"):
                try:
                    if os.path.getmtime(os.path.join(date_dir, f)) > video_mtime:
                        return True
                except OSError:
                    pass
        return False
    except OSError:
        return True  # Assume regen needed if storage is temporarily unreadable


def _run_video_generation(date_str, date_dir, video_path):
    """Generate a 720p H.264 MP4 from the JPEG frames for one date."""
    import shutil
    jpg_files = sorted(f for f in os.listdir(date_dir) if f.endswith(".jpg"))
    if not jpg_files:
        return False

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
            timeout=300,
        )
        if result.returncode == 0:
            shutil.copy2(tmp_out, video_path)
            return True
        return False
    except Exception:
        return False
    finally:
        for p in (filelist, tmp_out):
            try:
                os.unlink(p)
            except OSError:
                pass


def _generate_video_bg(date_str, date_dir, video_path):
    """Thread target: generate video then remove from active set."""
    try:
        _run_video_generation(date_str, date_dir, video_path)
    finally:
        with _video_gen_lock:
            _video_gen_active.discard(date_str)


@app.route("/api/timelapse/video/status", methods=["GET"])
@require_login
def api_timelapse_video_status():
    """Check whether the MP4 for a date is ready; kick off generation if not."""
    date_str = request.args.get("date", "")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return jsonify({"ok": False, "error": "Invalid date"}), 400

    date_dir = os.path.join(TIMELAPSE_DIR, date_str)
    try:
        if not os.path.isdir(date_dir):
            return jsonify({"ok": False, "error": "No images for this date"}), 404
    except OSError:
        return jsonify({"ok": False, "error": "Storage temporarily unavailable"}), 503

    video_path = os.path.join(date_dir, "timelapse.mp4")

    try:
        with _video_gen_lock:
            generating = date_str in _video_gen_active
            if not generating and _video_needs_regen(date_dir, video_path):
                _video_gen_active.add(date_str)
                threading.Thread(
                    target=_generate_video_bg,
                    args=(date_str, date_dir, video_path),
                    daemon=True,
                ).start()
                generating = True
    except Exception as exc:
        auth_logger.error("VIDEO_STATUS_ERROR date=%s — %s", date_str, exc)
        return jsonify({"ok": False, "error": "Internal error checking video status"}), 500

    return jsonify({"ok": True, "status": "generating" if generating else "ready"})


@app.route("/api/timelapse/video/file", methods=["GET"])
@require_login
def api_timelapse_video_file():
    """Serve the pre-generated timelapse MP4 for a date."""
    from flask import send_file

    date_str = request.args.get("date", "")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return jsonify({"ok": False, "error": "Invalid date"}), 400

    video_path = os.path.join(TIMELAPSE_DIR, date_str, "timelapse.mp4")
    if not os.path.isfile(video_path):
        return jsonify({"ok": False, "error": "Video not ready"}), 404

    return send_file(video_path, mimetype="video/mp4", conditional=True)


if __name__ == "__main__":
    auth_logger.info("Starting wcam-auth on 127.0.0.1:8086 (Waitress)")
    serve(app, host="127.0.0.1", port=8086, threads=4, connection_limit=100)
