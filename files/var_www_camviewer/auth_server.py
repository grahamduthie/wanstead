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
USERS_FILE = '/etc/nginx/.wcam-users.json'
SESSION_COOKIE_NAME = 'wcam_session'
SESSION_TTL = 86400  # 24 hours
USERNAME_RE = re.compile(r'^[a-zA-Z0-9_.-]{2,32}$')

# --- In-memory session store: token -> {username, is_admin, expires} ---
sessions = {}


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

    def __init__(self, filename, mode='a', maxBytes=0, backupCount=0,
                 when='midnight', interval=1, encoding='utf-8'):
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
        if self.when == 'midnight':
            # Compute next midnight in epoch seconds
            now = datetime.datetime.now()
            tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
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
            print(f'SAFE_HANDLER_BROKEN: cannot open {self._filename}', file=sys.stderr)

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
                print(f'SAFE_HANDLER_ROLLOVER_FAIL: {e}', file=sys.stderr)
                self._broken = True
                if self._stream and not self._stream.closed:
                    self._stream.close()
                self._stream = None
                return
        else:
            # RotatingFileHandler-style rotation
            if self.backupCount > 0:
                for i in range(self.backupCount - 1, 0, -1):
                    sfn = f'{self.baseFilename}.{i}'
                    dfn = f'{self.baseFilename}.{i + 1}'
                    if os.path.exists(sfn):
                        if os.path.exists(dfn):
                            os.remove(dfn)
                        os.rename(sfn, dfn)
                dfn = self.baseFilename + '.1'
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
                    raise OSError(f'Cannot open {self._filename}')

            if self.shouldRollover(record):
                self.doRollover()
                if self._broken:
                    raise OSError(f'Rollover failed for {self._filename}')

            if self._stream is None:
                raise OSError(f'No stream for {self._filename}')

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
            print(f'LOG_FALLBACK: {self.format(record)}', file=sys.stderr)


# --- Logging: auth events (fail2ban) ---
AUTH_LOG_PATH = '/var/log/wcam-auth.log'
auth_log_handler = SafeFileHandler(
    AUTH_LOG_PATH, maxBytes=1_000_000, backupCount=5,
    when=None, interval=1, encoding='utf-8'
)
auth_log_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
auth_log_handler.setLevel(logging.INFO)

auth_logger = logging.getLogger('wcam-auth')
auth_logger.addHandler(auth_log_handler)
auth_logger.setLevel(logging.INFO)

# --- Logging: login audit log (1 year retention) ---
AUDIT_LOG_PATH = '/var/log/wcam-login.log'
audit_log_handler = SafeFileHandler(
    AUDIT_LOG_PATH, when='midnight', interval=1, backupCount=365,
    encoding='utf-8'
)
audit_log_handler.setFormatter(logging.Formatter('%(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
audit_log_handler.setLevel(logging.INFO)

audit_logger = logging.getLogger('wcam-audit')
audit_logger.addHandler(audit_log_handler)
audit_logger.setLevel(logging.INFO)

# Silence Flask/Werkzeug request logging
logging.getLogger('werkzeug').setLevel(logging.ERROR)


# --- Helpers ---

def get_client_ip():
    """Get real client IP from X-Real-IP header (set by nginx) or fallback."""
    return request.headers.get('X-Real-IP', request.remote_addr)


def load_users():
    """Load user database from JSON file. Returns {username: {hash, is_admin}}."""
    try:
        with open(USERS_FILE, 'r') as f:
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
    tmp_path = USERS_FILE + '.tmp'
    try:
        with open(tmp_path, 'w') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(users, f, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
            fcntl.flock(f, fcntl.LOCK_UN)
        os.replace(tmp_path, USERS_FILE)
        return True
    except OSError as e:
        auth_logger.error('SAVE_USERS_FAILED: %s — user changes lost', e)
        print(f'SAVE_USERS_FAILED: {e}', file=sys.stderr)
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
        return bcrypt.checkpw(password.encode('utf-8'), user['hash'].encode('utf-8'))
    except Exception:
        return False


def create_session(username, is_admin):
    """Create a new session and return the token."""
    token = secrets.token_urlsafe(32)
    sessions[token] = {
        'username': username,
        'is_admin': is_admin,
        'expires': time.time() + SESSION_TTL
    }
    # Clean expired sessions
    now = time.time()
    expired = [t for t, s in sessions.items() if s['expires'] < now]
    for t in expired:
        del sessions[t]
    return token


def verify_session(token):
    """Verify a session token. Returns {username, is_admin} or None."""
    if not token:
        return None
    session = sessions.get(token)
    if not session:
        return None
    if session['expires'] < time.time():
        del sessions[token]
        return None
    return {'username': session['username'], 'is_admin': session['is_admin']}


def require_admin():
    """Check if the current request has a valid admin session. Returns user info or None."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    info = verify_session(token)
    if info and info['is_admin']:
        return info
    return None


def audit_log(event_type, username, ip, detail=''):
    """Write a structured JSON line to the audit log.
    The SafeFileHandler already falls back to stderr on I/O errors,
    but we add a secondary fallback to auth_log for extra safety.
    """
    entry = json.dumps({
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'event': event_type,
        'username': username,
        'ip': ip,
        'detail': detail
    })
    try:
        audit_logger.info(entry)
    except Exception:
        # Double safety: if SafeFileHandler's fallback itself fails
        print(f'AUDIT_LOG_FALLBACK: {entry}', file=sys.stderr)
        try:
            auth_logger.error('AUDIT_LOG: %s', entry)
        except Exception:
            pass  # Truly nowhere to write — stderr already got it


def check_filesystem_writable():
    """Check if the filesystem is writable. Returns (ok, error_string)."""
    test_path = '/var/log/.fs_write_test'
    try:
        with open(test_path, 'w') as f:
            f.write('1')
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
    status = 'ok'

    # Check for mmc0 errors in dmesg
    try:
        result = subprocess.run(
            ['dmesg', '-T'], capture_output=True, text=True, timeout=5
        )
        dmesg_output = result.stdout
        mmc_errors = len([l for l in dmesg_output.splitlines()
                         if 'mmc0' in l and any(k in l for k in ['error', 'timeout', 'reset', 'CRC'])])
        if mmc_errors > 0:
            issues.append(f'{mmc_errors} mmc0 errors in dmesg')
            status = 'warning'

        io_errors = len([l for l in dmesg_output.splitlines()
                        if any(k in l for k in ['I/O error', 'blk_update_request', 'Buffer I/O error'])])
        if io_errors > 0:
            issues.append(f'{io_errors} I/O errors in dmesg')
            status = 'critical'
    except Exception:
        pass  # dmesg may not be available

    # Check filesystem state
    try:
        result = subprocess.run(
            ['tune2fs', '-l', '/dev/mmcblk0p2'],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if line.startswith('Filesystem state:'):
                fs_state = line.split(':')[1].strip()
                if fs_state != 'clean':
                    issues.append(f'Filesystem state: {fs_state}')
                    status = 'critical'
            if line.startswith('Mount count:'):
                mount_count = int(line.split(':')[1].strip())
                issues.append(f'Mount count: {mount_count}')
            if line.startswith('Last checked:'):
                last_checked = line.split(':', 1)[1].strip()
                issues.append(f'Last checked: {last_checked}')
    except Exception:
        pass

    # Check for reboot recovery marker
    reboot_marker = '/var/log/.fs_recovery_reboot_pending'
    if os.path.exists(reboot_marker):
        age = time.time() - os.path.getmtime(reboot_marker)
        issues.append(f'Recovery reboot pending (marker age: {int(age)}s)')
        if status != 'critical':
            status = 'warning'

    return {
        'status': status,
        'issues': issues if issues else ['healthy']
    }


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
    if health['status'] == 'ok':
        audit_log('SD_CARD_HEALTH', 'system', '127.0.0.1', 'status=ok')
    else:
        detail = f"status={health['status']} issues={'; '.join(health['issues'])}"
        audit_log('SD_CARD_HEALTH', 'system', '127.0.0.1', detail)


# --- Health check endpoint ---

@app.route('/api/health', methods=['GET'])
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
    except Exception:
        pass  # Don't break health endpoint if audit logging fails

    status = {
        'ok': fs_ok and sd_health['status'] != 'critical',
        'filesystem_writable': fs_ok,
        'filesystem_error': fs_err,
        'sd_card': sd_health,
        'uptime': time.time()
    }
    http_code = 200 if status['ok'] else 503
    return jsonify(status), http_code


# --- Public API endpoints ---

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'ok': False, 'error': 'Invalid request'}), 400

    username = data.get('username', '').strip()
    password = data.get('password', '')
    client_ip = get_client_ip()

    if not username or not password:
        return jsonify({'ok': False, 'error': 'Username and password required'}), 400

    users = load_users()
    user = users.get(username)
    if user and verify_password(username, password):
        token = create_session(username, user.get('is_admin', False))
        auth_logger.info('LOGIN_OK user=%s ip=%s', username, client_ip)
        audit_log('LOGIN_OK', username, client_ip)
        resp = jsonify({'ok': True})
        resp.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            httponly=True,
            samesite='Lax',
            secure=True,
            max_age=SESSION_TTL,
            path='/'
        )
        return resp
    else:
        auth_logger.warning('LOGIN_FAIL user=%s ip=%s', username, client_ip)
        audit_log('LOGIN_FAIL', username, client_ip, 'invalid_credentials')
        return jsonify({'ok': False, 'error': 'Invalid username or password'}), 401


@app.route('/api/verify', methods=['GET'])
def api_verify():
    """Check if the session cookie is valid. Used by nginx auth_request."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    info = verify_session(token)
    if info:
        resp = Response(status=200)
        resp.headers['X-Auth-User'] = info['username']
        resp.headers['X-Auth-Admin'] = '1' if info['is_admin'] else '0'
        return resp
    return Response(status=401)


@app.route('/api/me', methods=['GET'])
def api_me():
    """Return current user info. Used by frontend to determine admin UI visibility."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    info = verify_session(token)
    if info:
        return jsonify({'ok': True, 'username': info['username'], 'is_admin': info['is_admin']})
    return jsonify({'ok': False}), 401


@app.route('/api/logout', methods=['POST'])
def api_logout():
    token = request.cookies.get(SESSION_COOKIE_NAME)
    info = verify_session(token)
    if info:
        audit_log('LOGOUT', info['username'], get_client_ip())
    if token and token in sessions:
        del sessions[token]
    resp = jsonify({'ok': True})
    resp.set_cookie(SESSION_COOKIE_NAME, '', expires=0, path='/')
    return resp


# --- Admin-only user management endpoints ---

@app.route('/api/users', methods=['GET'])
def api_list_users():
    """List all users (admin only). Returns usernames and admin flags, never hashes."""
    admin = require_admin()
    if not admin:
        return jsonify({'ok': False, 'error': 'Admin access required'}), 403

    users = load_users()
    result = []
    for uname, udata in sorted(users.items()):
        result.append({
            'username': uname,
            'is_admin': udata.get('is_admin', False)
        })
    return jsonify({'ok': True, 'users': result})


@app.route('/api/users', methods=['POST'])
def api_create_user():
    """Create a new user (admin only). Requires username, password, is_admin."""
    admin = require_admin()
    if not admin:
        return jsonify({'ok': False, 'error': 'Admin access required'}), 403

    data = request.get_json(silent=True)
    if not data:
        return jsonify({'ok': False, 'error': 'Invalid request'}), 400

    username = data.get('username', '').strip()
    password = data.get('password', '')
    is_admin = bool(data.get('is_admin', False))

    # Validate username format
    if not USERNAME_RE.match(username):
        return jsonify({'ok': False, 'error': 'Username must be 2-32 chars, alphanumeric/underscore/hyphen/dot only'}), 400

    # Validate password
    if len(password) < 4:
        return jsonify({'ok': False, 'error': 'Password must be at least 4 characters'}), 400

    users = load_users()
    if username in users:
        return jsonify({'ok': False, 'error': 'Username already exists'}), 409

    # Hash password with bcrypt
    password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt(rounds=10)).decode('utf-8')
    users[username] = {'hash': password_hash, 'is_admin': is_admin}
    if not save_users(users):
        return jsonify({'ok': False, 'error': 'Failed to save user — filesystem error'}), 500

    audit_log('USER_CREATED', admin['username'], get_client_ip(), f'user={username} admin={is_admin}')
    auth_logger.info('USER_CREATED by=%s user=%s admin=%s', admin['username'], username, is_admin)

    return jsonify({'ok': True, 'user': {'username': username, 'is_admin': is_admin}})


@app.route('/api/users/<target_username>', methods=['PUT'])
def api_update_user(target_username):
    """Update an existing user (admin only). Can change username, password, is_admin."""
    admin = require_admin()
    if not admin:
        return jsonify({'ok': False, 'error': 'Admin access required'}), 403

    data = request.get_json(silent=True)
    if not data:
        return jsonify({'ok': False, 'error': 'Invalid request'}), 400

    users = load_users()
    if target_username not in users:
        return jsonify({'ok': False, 'error': 'User not found'}), 404

    new_username = data.get('username', target_username).strip()
    new_password = data.get('password', '')
    new_is_admin = data.get('is_admin', users[target_username].get('is_admin', False))

    # Validate new username if changing
    if new_username != target_username:
        if not USERNAME_RE.match(new_username):
            return jsonify({'ok': False, 'error': 'Username must be 2-32 chars, alphanumeric/underscore/hyphen/dot only'}), 400
        if new_username in users:
            return jsonify({'ok': False, 'error': 'Username already exists'}), 409

    # Validate password if changing
    if new_password and len(new_password) < 4:
        return jsonify({'ok': False, 'error': 'Password must be at least 4 characters'}), 400

    # Build updated user record
    user_record = dict(users[target_username])
    if new_password:
        user_record['hash'] = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt(rounds=10)).decode('utf-8')
    user_record['is_admin'] = new_is_admin

    # If username is changing, delete old key and add new key
    if new_username != target_username:
        del users[target_username]
        users[new_username] = user_record
    else:
        users[target_username] = user_record

    if not save_users(users):
        return jsonify({'ok': False, 'error': 'Failed to save user — filesystem error'}), 500

    changes = []
    if new_username != target_username:
        changes.append(f'renamed {target_username}->{new_username}')
    if new_password:
        changes.append('password_changed')
    if 'is_admin' in data:
        changes.append(f'admin={new_is_admin}')

    audit_log('USER_UPDATED', admin['username'], get_client_ip(), f'target={new_username} changes={", ".join(changes)}')
    auth_logger.info('USER_UPDATED by=%s target=%s changes=%s', admin['username'], new_username, ', '.join(changes))

    return jsonify({'ok': True, 'user': {'username': new_username, 'is_admin': new_is_admin}})


@app.route('/api/users/<target_username>', methods=['DELETE'])
def api_delete_user(target_username):
    """Delete a user (admin only). Cannot delete the last admin user."""
    admin = require_admin()
    if not admin:
        return jsonify({'ok': False, 'error': 'Admin access required'}), 403

    users = load_users()
    if target_username not in users:
        return jsonify({'ok': False, 'error': 'User not found'}), 404

    # Prevent deleting the last admin
    admin_count = sum(1 for u in users.values() if u.get('is_admin', False))
    target_is_admin = users[target_username].get('is_admin', False)
    if target_is_admin and admin_count <= 1:
        return jsonify({'ok': False, 'error': 'Cannot delete the last admin user'}), 400

    del users[target_username]
    if not save_users(users):
        return jsonify({'ok': False, 'error': 'Failed to delete user — filesystem error'}), 500

    audit_log('USER_DELETED', admin['username'], get_client_ip(), f'deleted_user={target_username}')
    auth_logger.info('USER_DELETED by=%s target=%s', admin['username'], target_username)

    return jsonify({'ok': True})



@app.route('/api/audit', methods=['GET'])
def api_audit_log():
    """Return recent entries from the login audit log (admin only).
    Query params: page (default 1), per_page (default 50, max 200),
    event (optional filter), username (optional filter).
    Always reads rotated files so the most recent 50 entries are visible.
    """
    admin = require_admin()
    if not admin:
        return jsonify({'ok': False, 'error': 'Admin access required'}), 403

    page = max(1, int(request.args.get('page', 1)))
    per_page = min(200, max(10, int(request.args.get('per_page', 50))))
    event_filter = request.args.get('event', '').strip()
    username_filter = request.args.get('username', '').strip()
    has_filters = bool(event_filter or username_filter)

    import glob as glob_mod

    # Read all log files (current + rotated), sort by timestamp descending
    entries = []
    log_files = sorted(glob_mod.glob(AUDIT_LOG_PATH + '*'))
    if AUDIT_LOG_PATH in log_files:
        log_files.remove(AUDIT_LOG_PATH)
        log_files.insert(0, AUDIT_LOG_PATH)

    for log_file in log_files:
        try:
            with open(log_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if event_filter and entry.get('event') != event_filter:
                            continue
                        if username_filter and entry.get('username') != username_filter:
                            continue
                        entries.append(entry)
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            continue

    # Sort newest first by timestamp
    entries.sort(key=lambda e: e.get('timestamp', ''), reverse=True)

    total = len(entries)
    start = (page - 1) * per_page
    end = start + per_page
    page_entries = entries[start:end]

    return jsonify({
        'ok': True,
        'entries': page_entries,
        'total': total,
        'page': page,
        'per_page': per_page,
        'total_pages': (total + per_page - 1) // per_page if per_page else 1
    })

if __name__ == '__main__':
    auth_logger.info('Starting wcam-auth on 127.0.0.1:8086 (Waitress)')
    serve(app, host='127.0.0.1', port=8086, threads=4, connection_limit=100)
