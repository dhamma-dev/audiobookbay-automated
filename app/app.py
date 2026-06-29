import os, re, json, requests, atexit, shutil, socket, sqlite3, subprocess, tempfile, threading, uuid
from datetime import datetime, timezone
from flask import Flask, request, render_template, jsonify, redirect, session, url_for
from bs4 import BeautifulSoup
from qbittorrentapi import Client
from transmission_rpc import Client as transmissionrpc
from deluge_web_client import DelugeWebClient as delugewebclient
from dotenv import load_dotenv
from urllib.parse import urlparse
import secrets

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(16))

#Load environment variables
load_dotenv()

ABB_HOSTNAME = os.getenv("ABB_HOSTNAME", "audiobookbay.lu")

# Optional outbound request timeout in seconds (e.g. REQUEST_TIMEOUT="45").
# Unset by default, which preserves the original behavior of waiting as long
# as the mirror needs. The search UI shows a spinner and always clears it
# when the response arrives, so a slow search can't leave the page "stuck".
# Set this only if you want a hard cap on how long a mirror may take.
_timeout_env = os.getenv("REQUEST_TIMEOUT")
REQUEST_TIMEOUT = float(_timeout_env) if _timeout_env else None

# --- Tor ---------------------------------------------------------------------
# AudiobookBay requests can be routed through Tor so the mirror only ever sees a
# Tor exit node, never the server's real IP. The app starts and manages its own
# Tor process (with a localhost control port so a circuit can be renewed on
# demand), and keeps both a Tor-proxied and a direct session around so each user
# can toggle between them at runtime from the UI.
#
#   USE_TOR              - DEFAULT route for new visitors: "true" (default) means
#                          route via Tor unless the user toggles to Direct; set
#                          "false" to default to Direct. Tor still runs either way
#                          so the toggle works -- to not run Tor at all, set
#                          TOR_AUTOSTART=false (and don't point at an external Tor).
#   TOR_AUTOSTART        - let the app launch its own tor process (default on).
#                          Disable to point at an already-running Tor instead
#                          (circuit renewal is then unavailable).
#   TOR_SOCKS_PORT       - SOCKS port to use / start Tor on (default 9050).
#   TOR_CONTROL_PORT     - control port for circuit renewal (default 9051).
#   TOR_BOOTSTRAP_TIMEOUT- seconds to wait for Tor to connect (default 90).
def _is_truthy(value):
    return value.lower() not in ("0", "false", "no", "off", "")

USE_TOR = _is_truthy(os.getenv("USE_TOR", "true"))
TOR_AUTOSTART = _is_truthy(os.getenv("TOR_AUTOSTART", "true"))
TOR_SOCKS_PORT = int(os.getenv("TOR_SOCKS_PORT", "9050"))
TOR_CONTROL_PORT = int(os.getenv("TOR_CONTROL_PORT", "9051"))
TOR_BOOTSTRAP_TIMEOUT = int(os.getenv("TOR_BOOTSTRAP_TIMEOUT", "90"))

_tor_process = None
_tor_ready = threading.Event()
_tor_data_dir = None          # where Tor writes its control auth cookie
_tor_available = False        # a Tor SOCKS proxy we can route requests through
_tor_managed = False          # we launched Tor ourselves, so we can renew circuits
_renew_lock = threading.Lock()

# Built at startup by init_outbound(): a plain session and (when Tor is up) a
# Tor-proxied one. scrape_session() picks between them per request.
DIRECT_SESSION = None
TOR_SESSION = None


def _socks_port_open(port):
    """Return True if something is already accepting connections on the port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _consume_tor_output(proc):
    """Drain Tor's stdout so its pipe never blocks, surfacing bootstrap progress
    and warnings in the app logs and flagging when it reaches 100%."""
    for line in proc.stdout:
        line = line.strip()
        if "Bootstrapped" in line or "[err]" in line or "[warn]" in line:
            print(f"[TOR] {line}")
        if "Bootstrapped 100%" in line:
            _tor_ready.set()
    _tor_ready.set()  # process ended -- unblock any waiter


def _stop_tor():
    if _tor_process and _tor_process.poll() is None:
        _tor_process.terminate()
        try:
            _tor_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _tor_process.kill()


def _start_tor():
    """Bring Tor up if possible and record whether it is usable/renewable. Never
    raises: if Tor can't be started the app simply runs in Direct-only mode and
    the UI reflects that."""
    global _tor_process, _tor_data_dir, _tor_available, _tor_managed

    # Someone already runs Tor on the SOCKS port -- reuse it, but we can't renew
    # a circuit we don't control.
    if _socks_port_open(TOR_SOCKS_PORT):
        print(f"[TOR] Reusing Tor already listening on 127.0.0.1:{TOR_SOCKS_PORT}")
        _tor_available = True
        _tor_managed = False
        return

    if not TOR_AUTOSTART:
        print("[TOR] No Tor on the SOCKS port and TOR_AUTOSTART is off; running Direct-only.")
        return

    tor_bin = shutil.which("tor")
    if not tor_bin:
        print("[TOR] 'tor' binary not found; running Direct-only. Install Tor to enable it.")
        return

    data_dir = tempfile.mkdtemp(prefix="abb-tor-")
    print(f"[TOR] Starting Tor (SOCKS 127.0.0.1:{TOR_SOCKS_PORT}, control {TOR_CONTROL_PORT})...")
    _tor_process = subprocess.Popen(
        [
            tor_bin,
            "--SocksPort", str(TOR_SOCKS_PORT),
            "--ControlPort", f"127.0.0.1:{TOR_CONTROL_PORT}",
            "--CookieAuthentication", "1",
            "--DataDirectory", data_dir,
            "--ClientOnly", "1",
            "--AvoidDiskWrites", "1",
            "--Log", "notice stdout",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    atexit.register(_stop_tor)
    threading.Thread(target=_consume_tor_output, args=(_tor_process,), daemon=True).start()

    if not _tor_ready.wait(timeout=TOR_BOOTSTRAP_TIMEOUT) or _tor_process.poll() is not None:
        print(f"[TOR] Tor did not bootstrap within {TOR_BOOTSTRAP_TIMEOUT}s; running Direct-only.")
        return

    _tor_data_dir = data_dir
    _tor_available = True
    _tor_managed = True
    print("[TOR] Tor is ready; circuit renewal is available.")


def _direct_session():
    return requests.Session()


def _tor_session():
    """A session that proxies through Tor. socks5h keeps DNS resolution on the Tor
    side too, so the hostname never leaks."""
    session = requests.Session()
    proxy = f"socks5h://127.0.0.1:{TOR_SOCKS_PORT}"
    session.proxies = {"http": proxy, "https": proxy}
    return session


def init_outbound():
    """Bring up Tor (if available) and build both scraping sessions. Called once
    at startup so everything is ready before the first request."""
    global DIRECT_SESSION, TOR_SESSION
    _start_tor()
    DIRECT_SESSION = _direct_session()
    TOR_SESSION = _tor_session() if _tor_available else None


def current_route_mode():
    """'tor' or 'direct' for the current request: the user's saved choice, or the
    USE_TOR default. Forced to 'direct' whenever Tor isn't available."""
    if not _tor_available:
        return 'direct'
    mode = session.get('route_mode')
    if mode not in ('tor', 'direct'):
        mode = 'tor' if USE_TOR else 'direct'
    return mode


def scrape_session():
    """The requests session to use for AudiobookBay, per the active route mode."""
    if current_route_mode() == 'tor' and TOR_SESSION is not None:
        return TOR_SESSION
    return DIRECT_SESSION


def renew_tor_circuit():
    """Ask Tor for a fresh circuit (new exit) via the control port, then rebuild
    the Tor session so pooled connections don't keep the old circuit alive.
    Returns (ok, message)."""
    global TOR_SESSION
    if not (_tor_available and _tor_managed):
        return False, "Tor isn't running under this app's control, so its circuit can't be renewed."

    with _renew_lock:
        try:
            with open(os.path.join(_tor_data_dir, "control_auth_cookie"), "rb") as f:
                cookie_hex = f.read().hex()
            with socket.create_connection(("127.0.0.1", TOR_CONTROL_PORT), timeout=10) as ctrl:
                ctrl.settimeout(10)
                ctrl.sendall(f"AUTHENTICATE {cookie_hex}\r\n".encode())
                if not ctrl.recv(1024).decode(errors="replace").startswith("250"):
                    return False, "Tor control authentication failed."
                ctrl.sendall(b"SIGNAL NEWNYM\r\n")
                if not ctrl.recv(1024).decode(errors="replace").startswith("250"):
                    return False, "Tor did not accept the new-circuit request."
            # Drop pooled connections so the next request opens a fresh circuit.
            TOR_SESSION = _tor_session()
            return True, "Requested a new Tor circuit."
        except Exception as e:
            return False, f"Could not renew Tor circuit: {e}"

DOWNLOAD_CLIENT = os.getenv("DOWNLOAD_CLIENT")
DL_URL = os.getenv("DL_URL")
if DL_URL:
    parsed_url = urlparse(DL_URL)
    DL_SCHEME = parsed_url.scheme
    DL_HOST = parsed_url.hostname
    DL_PORT = parsed_url.port
else:
    DL_SCHEME = os.getenv("DL_SCHEME", "http")
    DL_HOST = os.getenv("DL_HOST")
    DL_PORT = os.getenv("DL_PORT")

    # Make a DL_URL for Deluge if one was not specified
    if DL_HOST and DL_PORT:
        DL_URL = f"{DL_SCHEME}://{DL_HOST}:{DL_PORT}"

DL_USERNAME = os.getenv("DL_USERNAME")
DL_PASSWORD = os.getenv("DL_PASSWORD")
DL_CATEGORY = os.getenv("DL_CATEGORY", "Audiobookbay-Audiobooks")
SAVE_PATH_BASE = os.getenv("SAVE_PATH_BASE")

# put.io credentials
# put.io supports two ways to authenticate:
#  1. OAuth login flow (the in-app "Log in with Put.io" button). Requires an
#     OAuth app registered on put.io -> set PUTIO_CLIENT_ID / PUTIO_CLIENT_SECRET.
#     The resulting per-user token is stored in the Flask session.
#  2. A static application-specific token via PUTIO_ACCESS_TOKEN (no login needed).
# If both are present the OAuth session token wins; otherwise whichever is set is
# used. See get_putio_token().
PUTIO_CLIENT_ID = os.getenv("PUTIO_CLIENT_ID")
PUTIO_CLIENT_SECRET = os.getenv("PUTIO_CLIENT_SECRET")
PUTIO_REDIRECT_URI = os.getenv("PUTIO_REDIRECT_URI")  # Optional fallback redirect URI
PUTIO_ACCESS_TOKEN = os.getenv("PUTIO_ACCESS_TOKEN")  # Application-specific password
PUTIO_SAVE_PARENT_ID = os.getenv("PUTIO_SAVE_PARENT_ID")  # Default folder ID to save to


def get_putio_token():
    """Return the active put.io token: the OAuth token from the user's session if
    they logged in, otherwise the static PUTIO_ACCESS_TOKEN env var (if set)."""
    return session.get('putio_access_token') or PUTIO_ACCESS_TOKEN

# Custom Nav Link Variables
NAV_LINK_NAME = os.getenv("NAV_LINK_NAME")
NAV_LINK_URL = os.getenv("NAV_LINK_URL")

# --- Smart sort (Gemini) -----------------------------------------------------
# Optional LLM re-ranking of search results. The mirror's own search is noisy
# and often floats irrelevant posts to the top, so after results load the user
# can ask Gemini to re-sort them by how well they match the query (preferring
# M4B on ties) and to flag when the query is ambiguous.
#
# This call goes straight to Google's API -- it is NOT routed through Tor (Tor
# only shields the AudiobookBay scraping). Only the already-scraped result
# metadata is sent: title, format, bitrate, language, size and keywords. No
# links, covers or hostnames leave the box. The feature is hidden entirely
# unless GEMINI_API_KEY is set.
#
#   GEMINI_API_KEY - Google AI Studio API key. Enables the feature when set.
#   RANK_MODEL     - Gemini model id to use (default "gemini-3.5-flash").
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
RANK_MODEL = os.getenv("RANK_MODEL", "gemini-3.5-flash")

# --- Download log ------------------------------------------------------------
# Records every send (who added what, when) so the operator can audit a shared
# instance. Identity comes from the reverse proxy's forwarded auth headers (see
# current_user_label) -- with Authentik that's the username. Stored in a small
# SQLite file; mount LOG_DB_PATH on a volume to keep history across restarts.
#
#   LOG_DB_PATH     - SQLite file path (default "/data/downloads.db"). Set empty
#                     to disable logging entirely.
#   LOG_ADMIN_USERS - comma-separated usernames allowed to see everyone's
#                     entries on /log. If unset, the page is open to all. Anyone
#                     not listed only ever sees their own additions.
LOG_DB_PATH = os.getenv("LOG_DB_PATH", "/data/downloads.db")
LOG_ADMIN_USERS = {u.strip() for u in os.getenv("LOG_ADMIN_USERS", "").split(",") if u.strip()}
LOG_ENABLED = bool(LOG_DB_PATH)
_log_lock = threading.Lock()


def _log_connect():
    conn = sqlite3.connect(LOG_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_download_log():
    """Create the log table if logging is enabled. Non-fatal on failure -- the
    app keeps working, it just won't record."""
    if not LOG_ENABLED:
        print("Download log: disabled (LOG_DB_PATH empty)")
        return
    try:
        os.makedirs(os.path.dirname(LOG_DB_PATH) or ".", exist_ok=True)
        with _log_lock, _log_connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    user TEXT NOT NULL,
                    title TEXT NOT NULL,
                    link TEXT,
                    infohash TEXT,
                    client TEXT,
                    route TEXT,
                    status TEXT NOT NULL,
                    detail TEXT
                )
            """)
            _ensure_log_columns(conn)
        admins = ', '.join(sorted(LOG_ADMIN_USERS)) if LOG_ADMIN_USERS else None
        print(f"Download log: {LOG_DB_PATH}" + (f" (admins: {admins})" if admins else " (viewable by everyone)"))
    except Exception as e:
        print(f"[WARNING] Download log unavailable ({LOG_DB_PATH}): {e}")


def _ensure_log_columns(conn):
    """Add columns introduced after the table's first release. SQLite's
    ADD COLUMN is cheap and additive, so existing rows just get NULLs and old
    databases migrate themselves on the next boot."""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(downloads)")}
    for col in ("batch_id", "batch_label"):
        if col not in existing:
            conn.execute(f"ALTER TABLE downloads ADD COLUMN {col} TEXT")


def record_download(user, title, link, infohash, status, detail="",
                    batch_id=None, batch_label=None):
    """Append one log row. Swallows storage errors so a logging hiccup can never
    fail an otherwise-successful download. batch_id/batch_label are set only for
    series "send selected" batches; single sends leave them NULL."""
    if not LOG_ENABLED:
        return
    try:
        with _log_lock, _log_connect() as conn:
            conn.execute(
                "INSERT INTO downloads (ts, user, title, link, infohash, client, route, status, detail, batch_id, batch_label)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), user, title, link,
                 infohash, DOWNLOAD_CLIENT, current_route_mode(), status, detail or "",
                 batch_id, batch_label),
            )
    except Exception as e:
        print(f"[WARNING] Failed to write download log entry: {e}")


def fetch_download_log(user_filter=None, limit=500):
    if not LOG_ENABLED:
        return []
    try:
        with _log_connect() as conn:
            if user_filter:
                rows = conn.execute("SELECT * FROM downloads WHERE user = ? ORDER BY id DESC LIMIT ?",
                                    (user_filter, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM downloads ORDER BY id DESC LIMIT ?",
                                    (limit,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[WARNING] Failed to read download log: {e}")
        return []


def is_log_admin(user):
    """Admins (or everyone, if no allowlist is set) can see all entries."""
    return (not LOG_ADMIN_USERS) or (user in LOG_ADMIN_USERS)


def _infohash_from_magnet(magnet_link):
    m = re.search(r'btih:([0-9a-zA-Z]+)', magnet_link or '')
    return m.group(1).lower() if m else None

# Fields we are willing to forward to Gemini. Everything else (link, cover,
# is_m4b, etc.) is dropped before the request is built.
RANK_FIELDS = ("title", "format", "bitrate", "language", "size", "keywords")

# --- Download client selection -----------------------------------------------
# The app sends magnets to exactly one download client, chosen at deploy time via
# DOWNLOAD_CLIENT. Selection is operator-level: run one instance per client.
# DOWNLOAD_CLIENT is required -- there is no default -- so a misconfigured deploy
# fails loudly (startup log + an in-app banner) instead of silently doing nothing.
CLIENT_LABELS = {
    'qbittorrent': 'qBittorrent',
    'transmission': 'Transmission',
    'delugeweb': 'Deluge',
    'putio': 'Put.io',
}

# Env each client must have to be usable. put.io authenticates per request (OAuth
# or static token), so its readiness is handled by the "Connect Put.io" banner
# rather than a hard config check here.
CLIENT_REQUIRED_ENV = {
    'qbittorrent': ('DL_HOST', 'DL_PORT', 'DL_USERNAME', 'DL_PASSWORD'),
    'transmission': ('DL_HOST', 'DL_PORT', 'DL_USERNAME', 'DL_PASSWORD'),
    'delugeweb': ('DL_URL', 'DL_PASSWORD'),
    'putio': (),
}

_ENV_VALUES = {
    'DL_HOST': DL_HOST, 'DL_PORT': DL_PORT, 'DL_USERNAME': DL_USERNAME,
    'DL_PASSWORD': DL_PASSWORD, 'DL_URL': DL_URL,
}


def _validate_client_config():
    """Return (ok, error_message). DOWNLOAD_CLIENT is required, must name a
    supported client, and that client must have the env it needs."""
    choices = ", ".join(CLIENT_LABELS)
    if not DOWNLOAD_CLIENT:
        return False, f"No download client configured. Set DOWNLOAD_CLIENT to one of: {choices}."
    if DOWNLOAD_CLIENT not in CLIENT_LABELS:
        return False, f"Unknown DOWNLOAD_CLIENT '{DOWNLOAD_CLIENT}'. Choose one of: {choices}."
    missing = [name for name in CLIENT_REQUIRED_ENV[DOWNLOAD_CLIENT] if not _ENV_VALUES.get(name)]
    if missing:
        return False, (f"{CLIENT_LABELS[DOWNLOAD_CLIENT]} is selected but these required "
                       f"settings are missing: {', '.join(missing)}.")
    return True, ""


CLIENT_OK, CLIENT_CONFIG_ERROR = _validate_client_config()

#Print configuration
print(f"ABB_HOSTNAME: {ABB_HOSTNAME}")
print(f"USE_TOR (default route): {'Tor' if USE_TOR else 'Direct'}" + (f", SOCKS 127.0.0.1:{TOR_SOCKS_PORT}" if TOR_AUTOSTART else ""))
print(f"DOWNLOAD_CLIENT: {DOWNLOAD_CLIENT or '(not set)'}")
if not CLIENT_OK:
    print(f"[CONFIG ERROR] {CLIENT_CONFIG_ERROR}")
print(f"DL_HOST: {DL_HOST}")
print(f"DL_PORT: {DL_PORT}")
print(f"DL_URL: {DL_URL}")
print(f"DL_USERNAME: {DL_USERNAME}")
print(f"DL_CATEGORY: {DL_CATEGORY}")
print(f"SAVE_PATH_BASE: {SAVE_PATH_BASE}")
print(f"NAV_LINK_NAME: {NAV_LINK_NAME}")
print(f"NAV_LINK_URL: {NAV_LINK_URL}")
print(f"SMART_SORT (Gemini): {'Enabled (' + RANK_MODEL + ')' if GEMINI_API_KEY else 'Disabled'}")
if DOWNLOAD_CLIENT == 'putio':
    print(f"PUTIO_CLIENT_ID: {'Set' if PUTIO_CLIENT_ID else 'Not Set'}")
    print(f"PUTIO_CLIENT_SECRET: {'Set' if PUTIO_CLIENT_SECRET else 'Not Set'}")
    print(f"PUTIO_ACCESS_TOKEN: {'Set' if PUTIO_ACCESS_TOKEN else 'Not Set'}")
    print(f"PUTIO_SAVE_PARENT_ID: {PUTIO_SAVE_PARENT_ID}")


@app.context_processor
def inject_app_config():
    client = DOWNLOAD_CLIENT or ''
    is_putio = client == 'putio'
    putio_authenticated = is_putio and bool(get_putio_token())
    # OAuth login is offered when an OAuth app is configured.
    putio_oauth_available = is_putio and bool(PUTIO_CLIENT_ID and PUTIO_CLIENT_SECRET)
    # The user logged in via the OAuth flow (vs. using a static env token).
    putio_session_login = is_putio and 'putio_access_token' in session
    return {
        'nav_link_name': os.getenv('NAV_LINK_NAME'),
        'nav_link_url': os.getenv('NAV_LINK_URL'),
        'download_client': client,
        'download_client_label': CLIENT_LABELS.get(client, 'Download Client'),
        # Shown app-wide when DOWNLOAD_CLIENT is unset/unknown or missing its env.
        'client_config_error': None if CLIENT_OK else CLIENT_CONFIG_ERROR,
        # Prompt for auth only when put.io is selected but we have no usable token.
        'show_putio_banner': is_putio and not putio_authenticated,
        'putio_authenticated': putio_authenticated,
        'putio_oauth_available': putio_oauth_available,
        'putio_session_login': putio_session_login,
        'smart_sort_available': bool(GEMINI_API_KEY),
        'log_enabled': LOG_ENABLED,
        # Connection controls (Tor routing toggle + circuit renewal).
        'tor_available': _tor_available,
        'tor_renewable': _tor_available and _tor_managed,
        'route_mode': current_route_mode(),
        'page_title_suffix': 'AudiobookBay',
    }


# --- Connection controls (Tor routing) ---------------------------------------
@app.route('/settings/route', methods=['POST'])
def set_route():
    """Persist this browser's AudiobookBay routing choice (tor|direct)."""
    data = request.get_json(silent=True) or {}
    mode = data.get('mode')
    if mode not in ('tor', 'direct'):
        return jsonify({'message': 'Invalid route mode.'}), 400
    if mode == 'tor' and not _tor_available:
        return jsonify({'message': 'Tor is not available on this server.'}), 409
    session['route_mode'] = mode
    session.permanent = True  # remember the choice across browser restarts
    return jsonify({'mode': mode})


@app.route('/tor/renew', methods=['POST'])
def tor_renew():
    """Request a fresh Tor circuit (new exit IP) for everyone on this instance."""
    ok, message = renew_tor_circuit()
    return jsonify({'message': message}), (200 if ok else 409)

# --- put.io OAuth routes -----------------------------------------------------
@app.route('/putio/auth')
def putio_auth():
    if DOWNLOAD_CLIENT != 'putio':
        return jsonify({'message': 'put.io is not configured as the download client'}), 400

    if not PUTIO_CLIENT_ID:
        return jsonify({'message': 'put.io client ID not configured'}), 400

    # Build the redirect URI from the current request so it works no matter what
    # host/port the app is reached on, and stash it for the callback.
    dynamic_redirect_uri = f"{request.host_url.rstrip('/')}/putio/callback"
    session['dynamic_redirect_uri'] = dynamic_redirect_uri

    auth_url = (
        "https://api.put.io/v2/oauth2/authenticate"
        f"?client_id={PUTIO_CLIENT_ID}&response_type=code&redirect_uri={dynamic_redirect_uri}"
    )
    print(f"[PUTIO] Starting OAuth with redirect URI: {dynamic_redirect_uri}")
    return redirect(auth_url)


@app.route('/putio/callback')
def putio_callback():
    if DOWNLOAD_CLIENT != 'putio':
        return jsonify({'message': 'put.io is not configured as the download client'}), 400

    code = request.args.get('code')
    if not code:
        return jsonify({'message': 'Authorization code not received'}), 400

    # Reuse the redirect URI from /putio/auth; fall back to the configured one.
    dynamic_redirect_uri = session.get('dynamic_redirect_uri') or PUTIO_REDIRECT_URI

    token_url = "https://api.put.io/v2/oauth2/access_token"
    data = {
        'client_id': PUTIO_CLIENT_ID,
        'client_secret': PUTIO_CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'redirect_uri': dynamic_redirect_uri,
        'code': code,
    }

    response = requests.post(token_url, data=data, timeout=REQUEST_TIMEOUT)
    if response.status_code != 200:
        return jsonify({'message': f'Failed to get access token: {response.text}'}), 400

    session['putio_access_token'] = response.json().get('access_token')
    return redirect(url_for('search'))


@app.route('/putio/logout')
def putio_logout():
    session.pop('putio_access_token', None)
    session.pop('dynamic_redirect_uri', None)
    return redirect(url_for('search'))


def search_audiobookbay(query, max_pages=5):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    sess = scrape_session()
    results = []
    for page in range(1, max_pages + 1):
        url = f"https://{ABB_HOSTNAME}/page/{page}/?s={query.replace(' ', '+')}"
        try:
            response = sess.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if response.status_code != 200:
                print(f"[ERROR] Failed to fetch page {page}. Status Code: {response.status_code}")
                break
            
            soup = BeautifulSoup(response.text, 'html.parser')
            posts = soup.select('.post')
            
            # If no posts found on this page, stop pagination
            if not posts:
                break
                
            for post in posts:
                try:
                    # Extract basic information
                    title_element = post.select_one('.postTitle > h2 > a')
                    if not title_element:
                        continue
                        
                    title = title_element.text.strip()
                    link = f"https://{ABB_HOSTNAME}{title_element['href']}"
                    cover = post.select_one('img')['src'] if post.select_one('img') else "/static/images/default-cover.svg"
                    
                    # Get post text and replace newlines with spaces to make regex easier
                    post_text = post.text.strip().replace('\n', ' ')
                    
                    # Extract file size
                    size = "Unknown"
                    size_pattern = r'Size: ([\d.]+\s*[KMGT]B)'
                    size_match = re.search(size_pattern, post_text)
                    if size_match:
                        size = size_match.group(1).strip()
                    
                    # Extract format information
                    format_info = "Unknown"
                    format_pattern = r'Format: ([^,]+)'
                    format_match = re.search(format_pattern, post_text)
                    if format_match:
                        format_info = format_match.group(1).strip()
                        # Check for MP3 / Bitrate format
                        if ' / ' in format_info:
                            format_info = format_info.split(' / ')[0].strip()
                    
                    # Extract bitrate - simplified
                    bitrate = "Unknown"
                    bitrate_pattern = r'Bitrate: ([^,]+)'
                    bitrate_match = re.search(bitrate_pattern, post_text)
                    if bitrate_match:
                        bitrate = bitrate_match.group(1).strip()
                        # Remove file size information if it got included
                        if 'File Size:' in bitrate:
                            bitrate = bitrate.split('File Size:')[0].strip()
                    
                    # Extract language information
                    language = "English"  # Default to English
                    language_pattern = r'Language: ([^,]+)'
                    language_match = re.search(language_pattern, post_text)
                    if language_match:
                        language = language_match.group(1).strip()
                        # Remove keywords if they got included
                        if 'Keywords:' in language:
                            language = language.split('Keywords:')[0].strip()
                    
                    # Extract keywords
                    keywords = []
                    keywords_pattern = r'Keywords: ([^\.]+)'
                    keywords_match = re.search(keywords_pattern, post_text)
                    if keywords_match:
                        keywords_text = keywords_match.group(1).strip()
                        keywords = [kw.strip() for kw in keywords_text.split(',')]
                    
                    # Flag M4B (the preferred single-file audiobook format).
                    # Check format, title and keywords since posts aren't
                    # consistent about where they mention it.
                    haystack = f"{title} {format_info} {' '.join(keywords)}".lower()
                    is_m4b = 'm4b' in haystack

                    results.append({
                        'title': title,
                        'link': link,
                        'cover': cover,
                        'size': size,
                        'format': format_info,
                        'bitrate': bitrate,
                        'language': language,
                        'keywords': keywords,
                        'is_m4b': is_m4b
                    })
                    
                except Exception as e:
                    print(f"[ERROR] Error parsing post: {e}")
                    continue
                    
        except Exception as e:
            print(f"[ERROR] Error fetching page {page}: {e}")
            break
            
    return results
# Helper function to extract magnet link from details page
def extract_magnet_link(details_url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    try:
        response = scrape_session().get(details_url, headers=headers, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            print(f"[ERROR] Failed to fetch details page. Status Code: {response.status_code}")
            return None

        soup = BeautifulSoup(response.text, 'html.parser')

        # Extract Info Hash
        info_hash_row = soup.find('td', string=re.compile(r'Info Hash', re.IGNORECASE))
        if not info_hash_row:
            print("[ERROR] Info Hash not found on the page.")
            return None
        info_hash = info_hash_row.find_next_sibling('td').text.strip()

        # Extract Trackers
        tracker_rows = soup.find_all('td', string=re.compile(r'udp://|http://', re.IGNORECASE))
        trackers = [row.text.strip() for row in tracker_rows]

        if not trackers:
            print("[WARNING] No trackers found on the page. Using default trackers.")
            trackers = [
                "udp://tracker.openbittorrent.com:80",
                "udp://opentor.org:2710",
                "udp://tracker.ccc.de:80",
                "udp://tracker.blackunicorn.xyz:6969",
                "udp://tracker.coppersurfer.tk:6969",
                "udp://tracker.leechers-paradise.org:6969"
            ]

        # Construct the magnet link
        trackers_query = "&".join(f"tr={requests.utils.quote(tracker)}" for tracker in trackers)
        magnet_link = f"magnet:?xt=urn:btih:{info_hash}&{trackers_query}"

        print(f"[DEBUG] Generated Magnet Link: {magnet_link}")
        return magnet_link

    except Exception as e:
        print(f"[ERROR] Failed to extract magnet link: {e}")
        return None

# Helper function for put.io operations
def send_to_putio(magnet_link, title=None):
    """
    Sends a magnet link to put.io using their API
    """
    token = get_putio_token()
    if not token:
        raise Exception("Put.io access token not configured")

    api_url = "https://api.put.io/v2/transfers/add"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}"
    }
    
    data = {
        "url": magnet_link
    }
    
    # Add parent folder ID if specified
    if PUTIO_SAVE_PARENT_ID:
        data["save_parent_id"] = PUTIO_SAVE_PARENT_ID
    
    response = requests.post(api_url, data=data, headers=headers, timeout=REQUEST_TIMEOUT)
    
    if response.status_code != 200:
        raise Exception(f"Put.io API error: {response.text}")
    
    return response.json()

# Helper function to get put.io transfer status
def get_putio_transfers():
    """
    Gets the list of transfers from put.io
    """
    token = get_putio_token()
    if not token:
        raise Exception("Put.io access token not configured")

    api_url = "https://api.put.io/v2/transfers/list"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}"
    }
    
    response = requests.get(api_url, headers=headers, timeout=REQUEST_TIMEOUT)
    
    if response.status_code != 200:
        raise Exception(f"Put.io API error: {response.text}")
    
    return response.json().get("transfers", [])

# Helper function to sanitize titles
def sanitize_title(title):
    return re.sub(r'[<>:"/\\|?*]', '', title).strip()


# --- Smart sort (Gemini re-ranking) ------------------------------------------
def rank_payload(books):
    """Build the slim, privacy-conscious payload sent to Gemini for re-ranking.

    Only the fields that actually help judge relevance are forwarded (see
    RANK_FIELDS) plus the stable id. Links, covers and the mirror hostname are
    deliberately excluded -- they tell the model nothing about the book and we
    have no reason to ship them off the box."""
    payload = []
    for book in books:
        item = {'id': book['id']}
        for field in RANK_FIELDS:
            value = book.get(field)
            if value:
                item[field] = value
        payload.append(item)
    return payload


RANK_SYSTEM_INSTRUCTION = (
    "You help a user sort noisy audiobook search results. You are given the "
    "user's search query and a list of candidate audiobooks (each with an id "
    "and metadata). The source site's search is unreliable and often returns "
    "irrelevant items, so your job is to rank the candidates by how well they "
    "match what the user is most likely looking for.\n"
    "Rules:\n"
    "- Rank strictly by relevance to the query (title/author/series match).\n"
    "- Break genuine ties by preferring M4B format, then higher bitrate.\n"
    "- Bucket each item: 'strong' (clearly matches), 'possible' (might match), "
    "or 'unlikely' (almost certainly not what they want).\n"
    "- If the query could plausibly refer to several distinct works or topics, "
    "set ambiguous=true and list the interpretations, each with the ids it "
    "covers. Otherwise set ambiguous=false and return an empty interpretations "
    "list.\n"
    "Series grouping:\n"
    "- If several candidates are entries in the same series, add a 'series' "
    "block: the series label, and an ordered list of entries (one per distinct "
    "book, in reading order). Use your own knowledge of the series to name and "
    "order the books and to give each a canonical title -- but every id you emit "
    "MUST be a real candidate id. Never invent a book that has no matching "
    "candidate, and only add a series block when two or more distinct books are "
    "genuinely present.\n"
    "- For each entry choose the single best edition as best_id and list the rest "
    "as alt_ids (best first). Selection rules, in order: prefer M4B; then a "
    "healthy bitrate; demote suspiciously low bitrate (<=64 kbps) and files whose "
    "size looks too small to be a complete book; never silently prefer an "
    "abridged, TTS/AI-narrated, or wrong-language upload over a clean full one.\n"
    "- When you demote an edition keep it in alt_ids and set alt_note to the axis "
    "it differs on (e.g. 'format', 'bitrate', 'abridged', 'language').\n"
    "- An abridged edition or a different narrator is its OWN entry, never an "
    "alternative of the unabridged one.\n"
    "- Number entries by their position in the series (seq). Include an entry for "
    "every book from book 1 up to the highest-numbered book present, in reading "
    "order. If a book in that run has no matching candidate, still include its "
    "entry (seq + title) but omit best_id -- that marks a gap. Never invent books "
    "beyond the highest one present.\n"
    "- If you confidently know the series' full length, set 'total' to it (so the "
    "UI can say 'X of Y'). Omit 'total' if unsure.\n"
    "- An omnibus/box-set/collection upload (one file spanning several books) "
    "goes in the 'collections' array, not in 'entries': give its id, a title, and "
    "'covers' = the list of book numbers (seq) it contains. Do not also use that "
    "id as an entry's best_id.\n"
    "- If nothing forms a series, return an empty 'series' list.\n"
    "Return every input id exactly once in 'ordering', best match first."
)

# JSON shape we ask Gemini to return. Kept flat and id-based so a chatty or
# truncated response still maps cleanly back onto the rendered cards.
RANK_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "ordering": {"type": "array", "items": {"type": "integer"}},
        "buckets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "bucket": {"type": "string",
                               "enum": ["strong", "possible", "unlikely"]},
                },
                "required": ["id", "bucket"],
            },
        },
        "ambiguous": {"type": "boolean"},
        "interpretations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "description": {"type": "string"},
                    "result_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["label", "result_ids"],
            },
        },
        "series": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "total": {"type": "integer"},
                    "entries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "seq": {"type": "integer"},
                                "title": {"type": "string"},
                                "best_id": {"type": "integer"},
                                "alt_ids": {"type": "array", "items": {"type": "integer"}},
                                "alt_note": {"type": "string"},
                            },
                            "required": ["seq", "title"],
                        },
                    },
                    "collections": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "integer"},
                                "title": {"type": "string"},
                                "covers": {"type": "array", "items": {"type": "integer"}},
                            },
                            "required": ["id", "title", "covers"],
                        },
                    },
                },
                "required": ["label", "entries"],
            },
        },
    },
    "required": ["ordering", "buckets", "ambiguous", "interpretations", "series"],
}


def rank_results(query, results):
    """Ask Gemini to re-rank already-scraped results. Returns the parsed JSON
    dict (ordering / buckets / ambiguous / interpretations). Raises on any
    transport or parsing failure so the caller can fall back to the existing
    order."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = (
        f"User search query: {query}\n\n"
        f"Candidates (JSON):\n{json.dumps(results, ensure_ascii=False)}"
    )
    response = client.models.generate_content(
        model=RANK_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=RANK_SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=RANK_RESPONSE_SCHEMA,
            temperature=0,
        ),
    )
    return json.loads(response.text)


@app.route('/api/rank', methods=['POST'])
def api_rank():
    """Re-rank a set of already-loaded results with Gemini. The client posts the
    query and the slim per-result metadata it was given; we never re-scrape the
    mirror here."""
    if not GEMINI_API_KEY:
        return jsonify({'message': 'Smart sort is not configured.'}), 503

    data = request.get_json(silent=True) or {}
    query = (data.get('query') or '').strip()
    incoming = data.get('results') or []
    if not query or not incoming:
        return jsonify({'message': 'Missing query or results.'}), 400

    # Re-sanitize on the server too: only ids and the allowed fields are ever
    # forwarded to Gemini, regardless of what the client sent.
    results = []
    for item in incoming:
        if not isinstance(item, dict) or 'id' not in item:
            continue
        slim = {'id': item['id']}
        for field in RANK_FIELDS:
            value = item.get(field)
            if value:
                slim[field] = value
        results.append(slim)

    if not results:
        return jsonify({'message': 'No rankable results.'}), 400

    try:
        ranking = rank_results(query, results)
        return jsonify(ranking)
    except Exception as e:
        print(f"[ERROR] Smart sort failed: {e}")
        return jsonify({'message': f'Smart sort failed: {e}'}), 502


# Endpoint for search page
@app.route('/', methods=['GET', 'POST'])
def search():
    books = []
    query = ''
    if request.method == 'POST':
        query = request.form.get('query', '').strip().lower()
        if query:
            books = search_audiobookbay(query)
            # Float the preferred M4B results to the top. Python's sort is
            # stable, so the mirror's original ordering is preserved within
            # the M4B and non-M4B groups.
            books.sort(key=lambda b: not b.get('is_m4b'))
    # Tag each result with a stable id so the client can ask Gemini to re-rank
    # them and then reorder the matching cards in place (see /api/rank).
    for i, book in enumerate(books):
        book['id'] = i
    m4b_count = sum(1 for b in books if b.get('is_m4b'))
    return render_template('search.html', books=books, query=query,
                           result_count=len(books), m4b_count=m4b_count,
                           rank_payload=rank_payload(books))


# --- Download backends -------------------------------------------------------
# One add/list pair per client. send() and the status page dispatch through
# DOWNLOAD_BACKENDS instead of branching inline, so adding a client is a single
# table entry. Connection objects are built per call (cheap, and avoids holding
# stale sessions); any transport error propagates to the caller for reporting.
class PutioNotConnected(Exception):
    """Raised when put.io is the client but no usable token is available."""
    def __init__(self):
        super().__init__('Put.io is not connected. Log in with Put.io or set PUTIO_ACCESS_TOKEN.')


def _torrent_save_path(title):
    """Per-book save path for the torrent clients. put.io ignores this and uses
    PUTIO_SAVE_PARENT_ID instead. Returns None when SAVE_PATH_BASE is unset so
    the client falls back to its own default download location."""
    return f"{SAVE_PATH_BASE}/{sanitize_title(title)}" if SAVE_PATH_BASE else None


def _qbittorrent_add(magnet_link, title):
    qb = Client(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
    qb.auth_log_in()
    qb.torrents_add(urls=magnet_link, save_path=_torrent_save_path(title), category=DL_CATEGORY)


def _qbittorrent_list():
    qb = Client(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
    qb.auth_log_in()
    return [
        {
            'name': t.name,
            'progress': round(t.progress * 100, 2),
            'state': t.state,
            'size': f"{t.total_size / (1024 * 1024):.2f} MB",
        }
        for t in qb.torrents_info(category=DL_CATEGORY)
    ]


def _transmission_add(magnet_link, title):
    client = transmissionrpc(host=DL_HOST, port=DL_PORT, protocol=DL_SCHEME,
                             username=DL_USERNAME, password=DL_PASSWORD)
    client.add_torrent(magnet_link, download_dir=_torrent_save_path(title))


def _transmission_list():
    client = transmissionrpc(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
    return [
        {
            'name': t.name,
            'progress': round(t.progress, 2),
            'state': t.status,
            'size': f"{t.total_size / (1024 * 1024):.2f} MB",
        }
        for t in client.get_torrents()
    ]


def _deluge_add(magnet_link, title):
    deluge = delugewebclient(url=DL_URL, password=DL_PASSWORD)
    deluge.login()
    deluge.add_torrent_magnet(magnet_link, save_directory=_torrent_save_path(title), label=DL_CATEGORY)


def _deluge_list():
    deluge = delugewebclient(url=DL_URL, password=DL_PASSWORD)
    deluge.login()
    torrents = deluge.get_torrents_status(
        filter_dict={"label": DL_CATEGORY},
        keys=["name", "state", "progress", "total_size"],
    )
    return [
        {
            'name': t["name"],
            'progress': round(t["progress"], 2),
            'state': t["state"],
            'size': f"{t['total_size'] / (1024 * 1024):.2f} MB",
        }
        for _, t in torrents.result.items()
    ]


def _putio_add(magnet_link, title):
    if not get_putio_token():
        raise PutioNotConnected()
    send_to_putio(magnet_link, title)


def _putio_list():
    if not get_putio_token():
        raise PutioNotConnected()
    return [
        {
            'name': tr.get('name', 'Unknown'),
            'progress': tr.get('percent_done', 0),
            'state': tr.get('status', 'Unknown'),
            'size': f"{tr.get('size', 0) / (1024 * 1024):.2f} MB",
        }
        for tr in get_putio_transfers()
    ]


DOWNLOAD_BACKENDS = {
    'qbittorrent': {'add': _qbittorrent_add, 'list': _qbittorrent_list},
    'transmission': {'add': _transmission_add, 'list': _transmission_list},
    'delugeweb': {'add': _deluge_add, 'list': _deluge_list},
    'putio': {'add': _putio_add, 'list': _putio_list},
}


# Endpoint to send magnet link to download client
@app.route('/send', methods=['POST'])
def send():
    data = request.json
    details_url = data.get('link')
    title = data.get('title')
    user = current_user_label()
    if not details_url or not title:
        return jsonify({'message': 'Invalid request'}), 400
    if not CLIENT_OK:
        return jsonify({'message': CLIENT_CONFIG_ERROR}), 503

    try:
        magnet_link = extract_magnet_link(details_url)
        if not magnet_link:
            record_download(user, title, details_url, None, 'error', 'Failed to extract magnet link')
            return jsonify({'message': 'Failed to extract magnet link'}), 500

        DOWNLOAD_BACKENDS[DOWNLOAD_CLIENT]['add'](magnet_link, title)
        record_download(user, title, details_url, _infohash_from_magnet(magnet_link), 'ok')
        return jsonify({'message': f'Download added successfully! This may take some time, the download will show in Audiobookshelf when completed.'})
    except PutioNotConnected as e:
        record_download(user, title, details_url, None, 'error', str(e))
        return jsonify({'message': str(e)}), 401
    except Exception as e:
        record_download(user, title, details_url, None, 'error', str(e))
        return jsonify({'message': str(e)}), 500


# Endpoint to send a whole set of magnets at once (series "send selected"). Each
# item is processed exactly like /send, but they share one batch_id in the log so
# the operator can see they were added together, and the response reports per-item
# so the UI can show partial success (a dead torrent shouldn't sink the rest).
@app.route('/send/batch', methods=['POST'])
def send_batch():
    data = request.json or {}
    items = data.get('items') or []                       # [{link, title}, ...]
    batch_label = (data.get('batch_label') or '').strip() or None
    user = current_user_label()
    if not items:
        return jsonify({'message': 'No items to send'}), 400
    if not CLIENT_OK:
        return jsonify({'message': CLIENT_CONFIG_ERROR}), 503

    batch_id = uuid.uuid4().hex[:12]
    results, sent = [], 0
    for item in items:
        link = (item.get('link') or '').strip()
        title = (item.get('title') or '').strip()
        if not link or not title:
            results.append({'link': link, 'title': title, 'ok': False, 'error': 'Missing link or title'})
            continue
        try:
            magnet_link = extract_magnet_link(link)
            if not magnet_link:
                record_download(user, title, link, None, 'error',
                                'Failed to extract magnet link', batch_id, batch_label)
                results.append({'link': link, 'title': title, 'ok': False, 'error': 'No magnet found'})
                continue
            DOWNLOAD_BACKENDS[DOWNLOAD_CLIENT]['add'](magnet_link, title)
            record_download(user, title, link, _infohash_from_magnet(magnet_link),
                            'ok', '', batch_id, batch_label)
            results.append({'link': link, 'title': title, 'ok': True})
            sent += 1
        except Exception as e:
            record_download(user, title, link, None, 'error', str(e), batch_id, batch_label)
            results.append({'link': link, 'title': title, 'ok': False, 'error': str(e)})

    return jsonify({'batch_id': batch_id, 'sent': sent, 'total': len(items), 'results': results})


def get_torrent_list():
    if not CLIENT_OK:
        raise ValueError(CLIENT_CONFIG_ERROR)
    return DOWNLOAD_BACKENDS[DOWNLOAD_CLIENT]['list']()


@app.route('/status')
def status():
    try:
        torrent_list = get_torrent_list()
        return render_template('status.html', torrents=torrent_list)
    except Exception as e:
        return render_template('status.html', torrents=[], error=str(e))


@app.route('/api/status')
def api_status():
    try:
        torrent_list = get_torrent_list()
        return jsonify({'torrents': torrent_list})
    except Exception as e:
        return jsonify({'message': f"Failed to fetch torrent status: {e}"}), 500


def current_user_label():
    """Best-effort identity for the download log: prefer Authentik's forwarded
    username, fall back through other common forward-auth headers, then the
    client IP. Reused by /whoami now and by the log when we build it."""
    h = request.headers
    return (h.get('X-authentik-username')
            or h.get('X-authentik-email')
            or h.get('Remote-User')                    # Authelia / generic forward-auth
            or h.get('X-Forwarded-Preferred-Username')
            or h.get('X-Forwarded-User')
            or request.remote_addr
            or 'unknown')


@app.route('/log')
def download_log():
    """Audit log of who sent what. Admins (or everyone, if no allowlist is set)
    see all entries and can filter by user; anyone else sees only their own."""
    user = current_user_label()
    admin = is_log_admin(user)
    # Non-admins are locked to their own rows regardless of any ?user= param.
    user_filter = (request.args.get('user') or None) if admin else user
    entries = fetch_download_log(user_filter=user_filter)
    return render_template('log.html', entries=entries, is_admin=admin,
                           user_filter=user_filter, log_enabled=LOG_ENABLED)


# Bring up Tor and the scraping session before serving any requests. Done at
# import time so it also covers WSGI servers, not just `python app.py`.
init_download_log()
init_outbound()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5078)
