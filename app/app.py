import os, re, json, requests, atexit, shutil, socket, sqlite3, subprocess, tempfile, threading, time, uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from flask import Flask, request, render_template, jsonify, redirect, session, url_for, has_request_context
from bs4 import BeautifulSoup
import abs_match
from qbittorrentapi import Client
from transmission_rpc import Client as transmissionrpc
from deluge_web_client import DelugeWebClient as delugewebclient
from dotenv import load_dotenv
from urllib.parse import urlparse, quote_plus
import secrets

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(16))

#Load environment variables
load_dotenv()

ABB_HOSTNAME = os.getenv("ABB_HOSTNAME", "audiobookbay.lu")

# Outbound request timeout in seconds for AudiobookBay fetches. Defaults to 45:
# a stalled Tor circuit was observed hanging a single page fetch (and therefore
# the whole search) for 16+ minutes, so "wait forever" is never what anyone
# wants. Set REQUEST_TIMEOUT=0 (or "off") to restore the old unbounded wait.
_timeout_env = (os.getenv("REQUEST_TIMEOUT") or "45").strip().lower()
REQUEST_TIMEOUT = None if _timeout_env in ("0", "off", "none") else float(_timeout_env)

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
_tor_starting = False         # our Tor is launched but not bootstrapped yet
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


def _await_tor_bootstrap(data_dir):
    """Wait (off the request path) for Tor to finish bootstrapping, then flip it
    to available and build the Tor session. On timeout/exit we stay Direct-only.
    Runs in a background thread so the web server is serving the whole time."""
    global _tor_data_dir, _tor_available, _tor_managed, _tor_starting, TOR_SESSION
    ok = _tor_ready.wait(timeout=TOR_BOOTSTRAP_TIMEOUT)
    if ok and _tor_process is not None and _tor_process.poll() is None:
        _tor_data_dir = data_dir
        _tor_managed = True
        TOR_SESSION = _tor_session()
        _tor_available = True
        print("[TOR] Tor is ready; circuit renewal is available.")
    else:
        print(f"[TOR] Tor did not bootstrap within {TOR_BOOTSTRAP_TIMEOUT}s; running Direct-only.")
    _tor_starting = False


def _start_tor():
    """Bring Tor up if possible and record whether it is usable/renewable. Never
    raises and never blocks: if we launch our own Tor, bootstrapping is awaited
    in a background thread so the app serves immediately (Direct works at once;
    Tor flips on when ready). The UI reflects 'starting' vs 'ready' meanwhile."""
    global _tor_process, _tor_available, _tor_managed, _tor_starting

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
    _tor_starting = True
    threading.Thread(target=_consume_tor_output, args=(_tor_process,), daemon=True).start()
    threading.Thread(target=_await_tor_bootstrap, args=(data_dir,), daemon=True).start()


def tor_status():
    """'ready' (route via Tor now), 'starting' (our Tor is still bootstrapping),
    or 'unavailable' (no Tor at all -> Direct-only)."""
    if _tor_available:
        return 'ready'
    if _tor_starting:
        return 'starting'
    return 'unavailable'


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
    """Build the Direct session and kick off Tor. Returns immediately: Direct is
    usable at once, and (when we manage Tor) the Tor session is built in the
    background as it finishes bootstrapping."""
    global DIRECT_SESSION, TOR_SESSION
    DIRECT_SESSION = _direct_session()
    _start_tor()
    # Reused external Tor is available synchronously; our own is built by
    # _await_tor_bootstrap once it's ready.
    if _tor_available and TOR_SESSION is None:
        TOR_SESSION = _tor_session()


def current_route_mode():
    """'tor' or 'direct' for the current request: the user's saved choice, or the
    USE_TOR default. Forced to 'direct' only when Tor is truly unavailable; while
    Tor is still 'starting' the intended mode is kept (search is gated until it's
    ready, or the user can switch to Direct)."""
    if tor_status() == 'unavailable':
        return 'direct'
    # Background work (the wanted-list worker) has no request; use the default.
    mode = session.get('route_mode') if has_request_context() else None
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
# Default per-browser behaviour for speculative smart-sort prefetch. "off" keeps
# today's behaviour (smart sort runs only when clicked); "on" starts it in the
# background as soon as results load so it's ready instantly. Each visitor can
# override this in the in-app settings.
SMART_PREFETCH_DEFAULT = "on" if _is_truthy(os.getenv("SMART_PREFETCH_DEFAULT", "off")) else "off"
# Reasoning budget for the ranking model. Gemini "flash" models do hidden
# thinking that dominates latency; benchmarking showed ranking is ~4-5x faster
# with a budget of 0 and no measurable quality loss (and far less variance), so
# 0 is the default. Set a positive N to allow that many thinking tokens, or a
# negative value to fall back to the model's own default thinking. If the model
# rejects a budget, rank_results retries once without it (and stops trying), so
# this can't hard-break sort.
_rank_think = os.getenv("RANK_THINKING_BUDGET")
if _rank_think in (None, ""):
    RANK_THINKING_BUDGET = 0
else:
    _v = int(_rank_think)
    RANK_THINKING_BUDGET = None if _v < 0 else _v
_thinking_supported = True  # flipped off if the model rejects a thinking budget

# Completed rankings are cached briefly so re-running the same search (a second
# tab, a page reload with prefetch on, a repeated query) doesn't pay Gemini --
# or its latency -- again. Stores the raw response text so every hit
# deserializes to a fresh object (resolve_ownership mutates its argument).
RANK_CACHE_TTL = int(os.getenv("RANK_CACHE_TTL", "900"))
_rank_cache = OrderedDict()  # key -> (expires_at, response_json_text)
_rank_cache_lock = threading.Lock()
_RANK_CACHE_MAX = 32


def _rank_cache_key(query, results, want_ownership):
    payload = json.dumps(results, sort_keys=True, ensure_ascii=False)
    return (query, want_ownership, RANK_MODEL, RANK_THINKING_BUDGET, hash(payload))


def _rank_cache_get(key):
    with _rank_cache_lock:
        hit = _rank_cache.get(key)
        if not hit:
            return None
        expires_at, text = hit
        if time.monotonic() > expires_at:
            del _rank_cache[key]
            return None
        _rank_cache.move_to_end(key)
        return text


def _rank_cache_put(key, text):
    with _rank_cache_lock:
        _rank_cache[key] = (time.monotonic() + RANK_CACHE_TTL, text)
        _rank_cache.move_to_end(key)
        while len(_rank_cache) > _RANK_CACHE_MAX:
            _rank_cache.popitem(last=False)

# Preferred listening language (e.g. "English"). When set, wrong-language
# editions are floated below matching ones in the default result order, and
# Smart sort is told to rank other languages far lower. Unset = no preference.
PREFERRED_LANGUAGE = (os.getenv("PREFERRED_LANGUAGE") or "").strip()


def _language_matches(book):
    """True when a result's language looks like the preferred one (or when no
    preference is configured). Substring + case-insensitive, since the mirror's
    language field is free text ("English", "english", "Eng")."""
    if not PREFERRED_LANGUAGE:
        return True
    lang = (book.get('language') or '').strip().lower()
    if not lang:
        return True  # unknown language -> don't penalize; let other signals decide
    pref = PREFERRED_LANGUAGE.lower()
    return pref in lang or lang in pref

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
            # Hardcover wanted-list pipeline state (see the Hardcover section).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS wanted (
                    hc_id INTEGER PRIMARY KEY,
                    title TEXT,
                    author TEXT,
                    slug TEXT,
                    status TEXT,
                    best_link TEXT,
                    best_title TEXT,
                    best_meta TEXT,
                    searched_at TEXT,
                    detail TEXT
                )
            """)
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(wanted)")}
            if "candidates" not in existing:  # added later: all STRONG matches, as JSON
                conn.execute("ALTER TABLE wanted ADD COLUMN candidates TEXT")
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


def _is_abb_link(url):
    """True only for links on the configured AudiobookBay host. The /send
    endpoints take a URL from the client; without this check any authenticated
    user could make the server fetch arbitrary URLs (SSRF) via the scrape
    session -- which, in Direct mode, originates from the server's real IP."""
    try:
        return (urlparse(url).hostname or '').lower() == ABB_HOSTNAME.lower()
    except ValueError:
        return False


# --- Audiobookshelf "in your library" matching -------------------------------
# Optional. When ABS_URL + ABS_TOKEN are set, each result we can confidently tie
# to something already in your Audiobookshelf library gets a discreet "In your
# library" badge. This is the deterministic *foundation*: precision-first (see
# abs_match.py), always-on, free, and private -- nothing leaves the box. It also
# serves as the cheap "blocker" (abs_match.candidates) that a future Smart-sort
# pass can hand to Gemini to resolve the harder cases. A missing badge is never
# a claim you DON'T own something.
#
#   ABS_URL         - Audiobookshelf base URL (e.g. https://audiobooks.example.com)
#   ABS_TOKEN       - ABS API token (Settings -> Users -> your user)
#   ABS_LIBRARY_ID  - optional; defaults to the first "book" library
#   ABS_CACHE_TTL   - seconds to cache the library index in memory (default 900)
ABS_URL = (os.getenv("ABS_URL") or "").strip().rstrip("/")
ABS_TOKEN = (os.getenv("ABS_TOKEN") or "").strip()
ABS_LIBRARY_ID = (os.getenv("ABS_LIBRARY_ID") or "").strip()
ABS_CACHE_TTL = int(os.getenv("ABS_CACHE_TTL", "900"))
ABS_MATCH_ENABLED = bool(ABS_URL and ABS_TOKEN)
_abs_cache = {"items": [], "fetched_at": 0.0}
_abs_lock = threading.Lock()


def _abs_get(path, **params):
    r = requests.get(f"{ABS_URL}{path}", headers={"Authorization": f"Bearer {ABS_TOKEN}"},
                     params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def _abs_pick_library():
    if ABS_LIBRARY_ID:
        return ABS_LIBRARY_ID
    libs = _abs_get("/api/libraries").get("libraries", [])
    for lib in libs:
        if lib.get("mediaType") == "book":
            return lib["id"]
    return libs[0]["id"] if libs else None


# --- Owned-copy quality ("Upgrade Radar") -------------------------------------
# Audiobookshelf doesn't expose a bitrate on the items listing, but it does give
# size (bytes) and duration (seconds) -- which is all we need: the *effective*
# bitrate of an owned copy is just size*8/duration. Everything here is local
# arithmetic on the cached index; nothing is sent anywhere.
#
#   ABS_LOW_KBPS - flag owned copies at or below this effective bitrate as
#                  upgrade candidates (default 63: spoken word is fine at 64+,
#                  while 32-48kbps rips are audibly poor).
ABS_LOW_KBPS = float(os.getenv("ABS_LOW_KBPS", "63"))
_FRAGMENTED_TRACKS = 8  # >= this many files reads as a per-chapter MP3 rip


def _quality_flag(item):
    """Return a short human reason when an owned copy looks below par, else
    None. Precision-first, like the matcher: unknown duration/size -> no flag."""
    reasons = []
    kbps, tracks = item.get("est_kbps"), item.get("tracks") or 0
    if kbps and kbps <= ABS_LOW_KBPS:
        reasons.append(f"~{int(round(kbps))} kbps")
    if tracks >= _FRAGMENTED_TRACKS:
        reasons.append(f"{tracks} files")
    return " · ".join(reasons) or None


def _parse_kbps(bitrate_str):
    m = re.search(r"([\d.]+)\s*kbps", bitrate_str or "", re.IGNORECASE)
    return float(m.group(1)) if m else None


def _is_upgrade_result(book, item):
    """Would this ABB result be a quality upgrade over the owned copy? True only
    when the owned copy is flagged AND the result is M4B AND its stated bitrate
    (when stated) isn't worse than what we already have."""
    if not item or not _quality_flag(item):
        return False
    if not book.get("is_m4b"):
        return False
    stated = _parse_kbps(book.get("bitrate"))
    owned = item.get("est_kbps")
    if stated and owned and stated < owned:
        return False
    return True


def _abs_load_items(library_id):
    items, page, limit = [], 0, 500
    while True:
        data = _abs_get(f"/api/libraries/{library_id}/items", limit=limit, page=page)
        results = data.get("results", [])
        for it in results:
            media = it.get("media") or {}
            md = media.get("metadata") or {}
            authors = md.get("authors") or []
            author = md.get("authorName") or ", ".join(a.get("name", "") for a in authors)
            series = [(s.get("name"), s.get("sequence")) for s in (md.get("series") or [])]
            size = media.get("size") or 0
            duration = media.get("duration") or 0
            tracks = media.get("numTracks") or media.get("numAudioFiles") or 0
            items.append({
                "title": md.get("title") or "",
                "author": author,
                "series": series,
                "asin": md.get("asin") or "",
                "isbn": md.get("isbn") or "",
                "language": md.get("language") or "",
                "size": size,
                "duration": duration,
                "tracks": tracks,
                # Effective bitrate of the copy on disk; None when unknown.
                "est_kbps": (size * 8 / duration / 1000) if size and duration else None,
            })
        if len(results) < limit or (data.get("total") and len(items) >= data["total"]):
            break
        page += 1
    return items


def get_abs_index(max_age=None):
    """Cached Audiobookshelf library items, refreshed once older than the cache
    TTL. `max_age` (seconds) lets a caller demand a fresher snapshot -- the
    ownership poll uses a short value so a just-finished download shows up
    quickly, while still fetching ABS at most once per that window regardless of
    how many polls arrive. Keeps the last good snapshot on error; returns []
    when disabled or not yet loaded."""
    if not ABS_MATCH_ENABLED:
        return []
    ttl = ABS_CACHE_TTL if max_age is None else min(ABS_CACHE_TTL, max_age)
    now = time.monotonic()
    with _abs_lock:
        if _abs_cache["items"] and now - _abs_cache["fetched_at"] < ttl:
            return _abs_cache["items"]
        try:
            lib = _abs_pick_library()
            items = _abs_load_items(lib) if lib else []
            _abs_cache.update(items=items, fetched_at=now)
            print(f"ABS match: indexed {len(items)} Audiobookshelf items")
        except Exception as e:
            print(f"[WARNING] Audiobookshelf library fetch failed: {e}")
            _abs_cache["fetched_at"] = now  # back off before retrying
        return _abs_cache["items"]


def annotate_library_matches(books):
    """Tag each result we confidently own with book['library_match']. Best-effort:
    any failure just leaves results unbadged, never breaks a search."""
    if not ABS_MATCH_ENABLED:
        return
    try:
        index = get_abs_index()
        if not index:
            return
        for book in books:
            raw = book.get("title", "")
            title, author = abs_match.split_title_author(raw)
            abb = {"raw": raw, "title": title, "author": author,
                   "language": book.get("language", "")}
            tier, score, item, _ = abs_match.best_match(abb, index)
            if tier == abs_match.STRONG and item:
                match = {"title": item["title"], "author": item["author"]}
                # Owned, but this result would improve on a below-par copy:
                # surface it as an invitation rather than a "you have this".
                if _is_upgrade_result(book, item):
                    match["upgrade"] = True
                    match["note"] = _quality_flag(item)
                book["library_match"] = match
    except Exception as e:
        print(f"[WARNING] Library match annotation failed: {e}")


def _norm_seq(seq):
    s = str(seq).strip()
    return s[:-2] if s.endswith(".0") else s


def _owned_series_index(index):
    """Map normalized series name -> {normalized seq: owned item}. Carrying the
    item (not just the number) lets upgrade detection see the owned copy's
    quality."""
    m = {}
    for item in index:
        for sname, seq in item.get("series") or []:
            if sname and seq is not None:
                m.setdefault(abs_match.normalize(sname), {})[_norm_seq(seq)] = item
    return m


def _owned_seqs_for(series_name, owned_series):
    """Fuzzy-match a canonical series name to the owned index; merge its
    {seq: item} maps."""
    target = abs_match.normalize(series_name or "")
    if not target:
        return {}
    seqs = {}
    for name, owned in owned_series.items():
        if name == target or abs_match.token_set_ratio(target, name) >= 0.8:
            seqs.update(owned)
    return seqs


def _canonical_owned(c, index, owned_series):
    """Resolve one canonical identity {title, author, series?, seq?} to the
    owned library item (None when not owned). Series books join exactly on
    (fuzzy series name, seq); everything else falls back to the local
    author-gated matcher. Shared by smart sort and the ownership poll."""
    series, seq = c.get("series"), c.get("seq")
    if series and seq is not None:
        hit = _owned_seqs_for(series, owned_series).get(_norm_seq(seq))
        if hit is not None:
            return hit
    abb = {"raw": c.get("title", ""), "title": c.get("title", ""),
           "author": c.get("author", ""), "language": c.get("language", "")}
    tier, _s, item, _r = abs_match.best_match(abb, index)
    return item if tier == abs_match.STRONG else None


def resolve_ownership(ranking, index, results=None):
    """Compute ownership LOCALLY, joining the LLM's canonicalized *public* results
    (ranking['canonical']) to the ABS index. Adds ranking['ownership'] =
    [{id, status, detail}]. Nothing about the library is ever sent out -- the LLM
    only supplied clean titles. Best-effort; never raises into the request.

    - Series books: exact join on (fuzzy series name, seq).
    - Standalones: the local author-gated matcher (abs_match).
    - Omnibus/box-set collections: covers intersected with owned seqs -> partial.
    - When `results` (the sanitized rank payload) is given, an owned result that
      would improve on a below-par copy is reported as 'upgrade' instead of
      'owned' (see _is_upgrade_result), so the UI can invite replacing junk.
    """
    if not isinstance(ranking, dict) or not index:
        return
    try:
        by_id = {r.get("id"): r for r in results or []}

        def _book_for(rid):
            r = by_id.get(rid) or {}
            fmt = (r.get("format") or "").lower()
            return {"is_m4b": "m4b" in fmt, "bitrate": r.get("bitrate")}

        def _verdict(rid, item):
            if rid in by_id and _is_upgrade_result(_book_for(rid), item):
                return {"status": "upgrade", "detail": _quality_flag(item)}
            return {"status": "owned"}

        owned_series = _owned_series_index(index)
        ownership = {}
        for c in ranking.get("canonical") or []:
            rid = c.get("id")
            if rid is None:
                continue
            item = _canonical_owned(c, index, owned_series)
            if item is not None:
                ownership[rid] = _verdict(rid, item)
        for block in ranking.get("series") or []:
            seqs = _owned_seqs_for(block.get("label", ""), owned_series)
            # Series members: the shelf already carries each book's number, so we
            # join (series, seq) locally -- no per-result canonical card needed.
            # Alternates are the same work as their entry, so they inherit its
            # ownership -- each judged for upgrade against its own format.
            for entry in block.get("entries") or []:
                eseq = entry.get("seq")
                if eseq is None:
                    continue
                item = seqs.get(_norm_seq(eseq))
                if item is None:
                    continue
                for rid in [entry.get("best_id")] + list(entry.get("alt_ids") or []):
                    if rid is not None:
                        ownership.setdefault(rid, _verdict(rid, item))
            for col in block.get("collections") or []:
                cid, covers = col.get("id"), col.get("covers") or []
                if cid is None or not covers:
                    continue
                owned_n = sum(1 for cv in covers if _norm_seq(cv) in seqs)
                if owned_n == len(covers):
                    ownership[cid] = {"status": "owned"}
                elif owned_n:
                    ownership[cid] = {"status": "partial", "detail": f"{owned_n} of {len(covers)}"}
        ranking["ownership"] = [dict(id=rid, **info) for rid, info in ownership.items()]
    except Exception as e:
        print(f"[WARNING] Ownership resolution failed: {e}")


print(f"ABS match: {'Enabled (' + ABS_URL + ')' if ABS_MATCH_ENABLED else 'Disabled'}")


# --- Hardcover wanted list -----------------------------------------------------
# Optional. With a Hardcover API key, the user's "Want to Read" list becomes a
# /wanted dashboard: each wanted book is pre-searched on ABB in the background
# and shown as Wanted -> Found -> Sent -> In library. Matching is entirely the
# deterministic matcher (Hardcover gives clean title+author, same shape as the
# ABS side), so the whole pipeline -- even fully automated -- costs ZERO Gemini
# tokens. Hardcover API notes (docs.hardcover.app): GraphQL at
# api.hardcover.app/v1/graphql, Bearer token, 60 req/min, tokens expire Jan 1,
# beta. We stay far under the rate limit (a couple of calls per sync) and send a
# descriptive user-agent, per their guidance. v1 is read-only toward Hardcover.
#
#   HARDCOVER_API_KEY    - token from hardcover.app account settings; enables it.
#   HARDCOVER_SYNC_TTL   - seconds between wanted-list refreshes (default 21600).
#   WANTED_RESEARCH_TTL  - seconds before an unfound book is searched again
#                          (default 86400 -- ABB inventory changes slowly).
#   WANTED_AUTO_DOWNLOAD - "true" to auto-send the best match. Strictest gate
#                          only: STRONG title+author match AND M4B. Default off.
#   WANTED_ROUTE         - route for BACKGROUND searches: "default" (the server's
#                          USE_TOR default), "tor", or "direct". The manual
#                          per-row re-check always uses YOUR browser's route
#                          toggle instead, so it doubles as a diagnostic: if
#                          re-check finds books the background can't, the
#                          background route's exit is being blocked.
HARDCOVER_API_KEY = (os.getenv("HARDCOVER_API_KEY") or "").strip()
HARDCOVER_SYNC_TTL = int(os.getenv("HARDCOVER_SYNC_TTL", "21600"))
WANTED_RESEARCH_TTL = int(os.getenv("WANTED_RESEARCH_TTL", "86400"))
WANTED_RETRY_TTL = int(os.getenv("WANTED_RETRY_TTL", "1800"))  # after a failed scrape
WANTED_AUTO_DOWNLOAD = _is_truthy(os.getenv("WANTED_AUTO_DOWNLOAD", "false"))
WANTED_ROUTE = (os.getenv("WANTED_ROUTE") or "default").strip().lower()
WANTED_ENABLED = bool(HARDCOVER_API_KEY)
_HARDCOVER_URL = "https://api.hardcover.app/v1/graphql"
_wanted_lock = threading.Lock()
_wanted_mem = {}          # hc_id -> row dict; fallback store when the log DB is off
_wanted_last_sync = 0.0
_wanted_sync_error = ""   # last sync failure, surfaced on the dashboard


def _hardcover_gql(query, variables=None):
    token = HARDCOVER_API_KEY
    if token and not token.lower().startswith("bearer "):
        token = f"Bearer {token}"
    r = requests.post(_HARDCOVER_URL,
                      json={"query": query, "variables": variables or {}},
                      headers={"authorization": token,
                               "content-type": "application/json",
                               "user-agent": "audiobookbay-automated (self-hosted wanted-list sync)"},
                      timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(data["errors"][0].get("message", "Hardcover API error"))
    return data.get("data") or {}


def _parse_wanted_payload(data):
    """Pure: Hardcover user_books payload -> [{hc_id, title, author, slug}]."""
    rows = []
    for ub in data.get("user_books") or []:
        b = ub.get("book") or {}
        if not b.get("title"):
            continue
        authors = [c.get("author", {}).get("name", "")
                   for c in (b.get("contributions") or []) if c.get("author")]
        rows.append({
            "hc_id": b.get("id"),
            "title": b["title"],
            "author": ", ".join(a for a in authors if a),
            "slug": b.get("slug") or "",
        })
    return [r for r in rows if r["hc_id"] is not None]


def _fetch_hardcover_wanted():
    me = _hardcover_gql("query { me { id } }").get("me")
    uid = (me[0] if isinstance(me, list) and me else me or {}).get("id")
    if not uid:
        raise RuntimeError("Couldn't resolve the Hardcover user id for this token.")
    data = _hardcover_gql(
        """query ($uid: Int!) {
             user_books(where: {user_id: {_eq: $uid}, status_id: {_eq: 1}}) {
               book { id title slug contributions { author { name } } }
             }
           }""", {"uid": uid})
    return _parse_wanted_payload(data)


# --- wanted-row store: SQLite when the download log is on, else in-memory ----
def _wanted_rows():
    if not LOG_ENABLED:
        with _wanted_lock:
            return [dict(r) for r in _wanted_mem.values()]
    with _log_lock, _log_connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM wanted").fetchall()]


def _wanted_upsert(row):
    if not LOG_ENABLED:
        with _wanted_lock:
            _wanted_mem[row["hc_id"]] = {**_wanted_mem.get(row["hc_id"], {}), **row}
        return
    cols = ("hc_id", "title", "author", "slug", "status", "best_link", "best_title",
            "best_meta", "searched_at", "detail", "candidates")
    with _log_lock, _log_connect() as conn:
        existing = conn.execute("SELECT * FROM wanted WHERE hc_id = ?", (row["hc_id"],)).fetchone()
        merged = {**(dict(existing) if existing else {}), **row}
        conn.execute(
            f"INSERT OR REPLACE INTO wanted ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            tuple(merged.get(c) for c in cols))


def _wanted_delete_missing(keep_ids):
    if not LOG_ENABLED:
        with _wanted_lock:
            for k in list(_wanted_mem):
                if k not in keep_ids:
                    del _wanted_mem[k]
        return
    with _log_lock, _log_connect() as conn:
        rows = conn.execute("SELECT hc_id FROM wanted").fetchall()
        for r in rows:
            if r["hc_id"] not in keep_ids:
                conn.execute("DELETE FROM wanted WHERE hc_id = ?", (r["hc_id"],))


def _wanted_mark_sent_by_link(link):
    """Called after a successful /send: if that link is a wanted book's pick OR
    any of its stored alternatives, advance the row so the dashboard follows
    the user's action whichever edition they chose."""
    if not (WANTED_ENABLED and link):
        return
    try:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for row in _wanted_rows():
            if row.get("status") not in ("found", "wanted"):
                continue
            links = {row.get("best_link")}
            try:
                links.update(c.get("link") for c in json.loads(row.get("candidates") or "[]"))
            except ValueError:
                pass
            if link in links:
                _wanted_upsert({"hc_id": row["hc_id"], "status": "sent",
                                "detail": f"sent {now}"})
    except Exception as e:
        print(f"[WARNING] wanted mark-sent failed: {e}")


# --- search + pick (all deterministic; no LLM anywhere in this pipeline) ------
def _wanted_queries(title, author):
    """Query ladder for one wanted book. ABB's search is an AND-ish full-text
    match, so the full Hardcover title -- subtitles and all ("The Last Wish:
    Introducing the Witcher") -- often matches nothing. Search BROAD and let the
    matcher pick the candidate: subtitle/parenthetical stripped, first with the
    author's surname for signal, then the bare short title as fallback."""
    short = re.split(r"[:(\[]", title)[0].strip() or title.strip()
    surname = ""
    if author:
        parts = author.split(",")[0].strip().split()
        surname = parts[-1] if parts else ""
    queries = [f"{short} {surname}"] if surname else []
    queries.append(short)
    seen, out = set(), []
    for q in (q.lower() for q in queries):
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out


# ABB "request" posts describe a book somebody is ASKING for -- there's no
# torrent behind them, so they must never become a pick.
_ABB_REQUEST_RE = re.compile(r"\(\s*REQ", re.IGNORECASE)


def _match_wanted_against(books, title, author):
    """STRONG-only matches of scraped ABB results against one clean wanted
    identity. Returns the matching book dicts (the wanted book acts as a
    one-item 'library' for the same author-gated matcher used everywhere)."""
    target = [{"title": title, "author": author, "series": [], "language": ""}]
    hits = []
    for b in books:
        raw = b.get("title", "")
        if _ABB_REQUEST_RE.search(raw):
            continue
        rt, ra = abs_match.split_title_author(raw)
        abb = {"raw": raw, "title": rt, "author": ra,
               "language": b.get("language", "")}
        tier, _s, _i, _r = abs_match.best_match(abb, target)
        if tier == abs_match.STRONG:
            hits.append(b)
    return hits


def _rank_wanted_candidates(hits):
    """STRONG matches ordered best-first: M4B first, preferred language, then
    stated bitrate. Deterministic counterpart of smart sort's edition rules;
    element 0 is the pick, the rest are the row's expandable alternatives."""
    def key(b):
        return (bool(b.get("is_m4b")), _language_matches(b),
                _parse_kbps(b.get("bitrate")) or 0)
    return sorted(hits, key=key, reverse=True)


def _candidate_payload(b):
    """The slim, render-ready slice of a matched result stored on the row."""
    return {"title": b.get("title"), "link": b.get("link"),
            "format": b.get("format"), "bitrate": b.get("bitrate"),
            "size": b.get("size"), "language": b.get("language"),
            "is_m4b": bool(b.get("is_m4b"))}


def _wanted_session():
    """Session for BACKGROUND wanted searches per WANTED_ROUTE. Manual re-checks
    pass sess=None so they follow the requesting browser's route toggle."""
    if WANTED_ROUTE == "direct":
        return DIRECT_SESSION
    if WANTED_ROUTE == "tor":
        return TOR_SESSION if TOR_SESSION is not None else DIRECT_SESSION
    return None  # "default": scrape_session() resolves it (server default)


def _wanted_route_is_tor():
    if WANTED_ROUTE == "tor":
        return True
    if WANTED_ROUTE == "direct":
        return False
    return USE_TOR and _tor_available


# Self-healing for a starved/blocked Tor exit: after a few consecutive
# unreachable scrapes on the Tor route, ask for a fresh circuit (rate-limited --
# renewal swaps the exit for everyone on the instance) and put the failed rows
# straight back in the queue instead of waiting out the retry TTL.
_WANTED_RENEW_AFTER = 3      # consecutive unreachable searches
_WANTED_RENEW_COOLDOWN = 600  # seconds between automatic renewals
_wanted_fail_streak = 0
_wanted_last_renew = 0.0


def _wanted_note_result(status):
    global _wanted_fail_streak
    _wanted_fail_streak = _wanted_fail_streak + 1 if status == "unreachable" else 0


def _wanted_maybe_renew():
    """Renew the Tor circuit when background searches keep failing on Tor.
    Returns True when a renewal happened (so failed rows can be requeued)."""
    global _wanted_fail_streak, _wanted_last_renew
    if _wanted_fail_streak < _WANTED_RENEW_AFTER or not _wanted_route_is_tor():
        return False
    if not (_tor_available and _tor_managed):
        return False
    if time.monotonic() - _wanted_last_renew < _WANTED_RENEW_COOLDOWN:
        return False
    ok, message = renew_tor_circuit()
    _wanted_last_renew = time.monotonic()
    _wanted_fail_streak = 0
    print(f"[WANTED] Tor exit looked blocked; circuit renewal: {message}")
    if ok:
        for row in _wanted_rows():
            if row.get("status") == "wanted" and "didn't respond" in (row.get("detail") or ""):
                _wanted_upsert({"hc_id": row["hc_id"], "searched_at": None})
    return ok


def wanted_route_label():
    if WANTED_ROUTE in ("tor", "direct"):
        return WANTED_ROUTE.capitalize()
    return ("Tor" if USE_TOR and _tor_available else "Direct") + " (server default)"


def _wanted_owned(title, author):
    if not ABS_MATCH_ENABLED:
        return False
    index = get_abs_index()
    if not index:
        return False
    tier, _s, _i, _r = abs_match.best_match(
        {"raw": title, "title": title, "author": author, "language": ""}, index)
    return tier == abs_match.STRONG


def _wanted_search_one(row, sess=None):
    """Search ABB for one wanted book and update its row. Returns new status.
    A failed scrape (mirror unreachable / blocked exit) is NOT "unmatched": the
    row drops back to 'wanted' with the error in detail and retries on the
    short WANTED_RETRY_TTL instead of the daily re-search cadence."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    title, author = row["title"], row.get("author") or ""
    try:
        if _wanted_owned(title, author):
            _wanted_upsert({"hc_id": row["hc_id"], "status": "owned", "searched_at": now})
            return "owned"
        considered, tried = 0, 0
        for q in _wanted_queries(title, author):
            tried += 1
            books = search_audiobookbay(q, max_pages=2, sess=sess)
            if books is None:
                print(f"[WANTED] {title!r}: ABB unreachable on this route; will retry")
                _wanted_upsert({"hc_id": row["hc_id"], "status": "wanted", "searched_at": now,
                                "detail": "AudioBook Bay didn't respond on the background "
                                          "route — retrying shortly"})
                return "unreachable"  # row status is 'wanted'; sentinel drives renewal
            considered += len(books)
            ranked = _rank_wanted_candidates(_match_wanted_against(books, title, author))
            if ranked:
                best = ranked[0]
                meta = " · ".join(x for x in (best.get("format"), best.get("bitrate"),
                                              best.get("size")) if x and x != "Unknown")
                _wanted_upsert({"hc_id": row["hc_id"], "status": "found",
                                "best_link": best.get("link"), "best_title": best.get("title"),
                                "best_meta": meta, "searched_at": now, "detail": "",
                                "candidates": json.dumps([_candidate_payload(b)
                                                          for b in ranked[:8]])})
                print(f"[WANTED] {title!r}: found via {q!r} ({meta}; "
                      f"{len(ranked)} candidate(s))")
                return "found"
        _wanted_upsert({"hc_id": row["hc_id"], "status": "unmatched", "searched_at": now,
                        "detail": f"no confident match ({tried} searches, "
                                  f"{considered} results considered)"})
        print(f"[WANTED] {title!r}: no match ({tried} searches, {considered} results)")
        return "unmatched"
    except Exception as e:
        print(f"[WARNING] wanted search failed for {title!r}: {e}")
        _wanted_upsert({"hc_id": row["hc_id"], "status": "wanted",
                        "searched_at": now, "detail": str(e)})
        return "wanted"


def _wanted_auto_send(row):
    """Auto-download a found match under the strictest gate (M4B was already
    required by the picker order? No -- enforce it here explicitly)."""
    try:
        link, title = row.get("best_link"), row.get("best_title") or row["title"]
        if not (link and CLIENT_OK):
            return
        if "m4b" not in (row.get("best_meta") or "").lower():
            return  # auto only ever grabs the recommended single-file format
        magnet = extract_magnet_link(link)
        if not magnet:
            record_download("hardcover-auto", title, link, None, "error",
                            "Failed to extract magnet link")
            return
        DOWNLOAD_BACKENDS[DOWNLOAD_CLIENT]["add"](magnet, title)
        record_download("hardcover-auto", title, link, _infohash_from_magnet(magnet),
                        "ok", "Wanted-list auto-download")
        _wanted_upsert({"hc_id": row["hc_id"], "status": "sent",
                        "detail": "auto-downloaded"})
        print(f"[WANTED] auto-sent {title!r}")
    except Exception as e:
        print(f"[WARNING] wanted auto-send failed for {row.get('title')!r}: {e}")
        record_download("hardcover-auto", row.get("best_title") or row["title"],
                        row.get("best_link"), None, "error", str(e))


def _wanted_sync_list():
    """Refresh the wanted list from Hardcover, upserting new books and dropping
    ones the user removed (their Hardcover list stays the source of truth)."""
    global _wanted_last_sync, _wanted_sync_error
    wanted = _fetch_hardcover_wanted()
    keep = set()
    for w in wanted:
        keep.add(w["hc_id"])
        _wanted_upsert({"hc_id": w["hc_id"], "title": w["title"],
                        "author": w["author"], "slug": w["slug"]})
    # New rows need a status; don't clobber rows already progressed.
    for row in _wanted_rows():
        if row["hc_id"] in keep and not row.get("status"):
            _wanted_upsert({"hc_id": row["hc_id"], "status": "wanted"})
    _wanted_delete_missing(keep)
    _wanted_last_sync = time.monotonic()
    _wanted_sync_error = ""
    print(f"[WANTED] synced {len(wanted)} wanted books from Hardcover")


def _wanted_due_rows():
    """Rows that need (re)searching: never searched, or past their cadence.
    'wanted' rows (not yet successfully searched, incl. failed scrapes) retry on
    the short WANTED_RETRY_TTL; successfully-searched rows use the daily TTL."""
    due = []
    now = datetime.now(timezone.utc)
    for row in _wanted_rows():
        status = row.get("status") or "wanted"
        if status in ("sent", "owned"):
            continue
        searched = row.get("searched_at")
        if not searched:
            due.append(row)
            continue
        ttl = WANTED_RETRY_TTL if status == "wanted" else WANTED_RESEARCH_TTL
        try:
            age = (now - datetime.fromisoformat(searched)).total_seconds()
        except ValueError:
            age = ttl + 1
        if age > ttl:
            due.append(row)
    return due


def _wanted_worker():
    """Background loop: keep the wanted list synced and searched. Deliberately
    gentle -- at most 3 ABB searches per minute-tick, all through the default
    route (Tor when available), and a couple of Hardcover calls per sync TTL."""
    global _wanted_sync_error
    while True:
        try:
            if time.monotonic() - _wanted_last_sync > HARDCOVER_SYNC_TTL or _wanted_last_sync == 0:
                try:
                    _wanted_sync_list()
                except Exception as e:
                    _wanted_sync_error = str(e)
                    print(f"[WARNING] Hardcover sync failed: {e}")
            if tor_status() in ("ready", "unavailable"):  # don't scrape mid-bootstrap
                due = _wanted_due_rows()
                if due:
                    print(f"[WANTED] {len(due)} book(s) due; searching up to 3 "
                          f"via {wanted_route_label()}", flush=True)
                for row in due[:3]:
                    status = _wanted_search_one(row, sess=_wanted_session())
                    _wanted_note_result(status)
                    if status == "unreachable":
                        break  # this route is down right now; stop burning the tick
                    if status == "found" and WANTED_AUTO_DOWNLOAD:
                        fresh = next((r for r in _wanted_rows()
                                      if r["hc_id"] == row["hc_id"]), None)
                        if fresh:
                            _wanted_auto_send(fresh)
                _wanted_maybe_renew()
        except Exception as e:
            print(f"[WARNING] wanted worker tick failed: {e}")
        time.sleep(60)


def _wanted_requeue_open():
    """Mark every open row due for a fresh search. Run at boot so a restart
    always sweeps the list -- otherwise rows stamped by an older (possibly
    buggier) build sit out their whole retry backoff before anything visible
    happens. Sent/owned rows are settled and stay put."""
    n = 0
    for row in _wanted_rows():
        if row.get("status") not in ("sent", "owned") and row.get("searched_at"):
            _wanted_upsert({"hc_id": row["hc_id"], "searched_at": None})
            n += 1
    return n


def init_wanted():
    if not WANTED_ENABLED:
        print("Hardcover wanted list: disabled (no HARDCOVER_API_KEY)")
        return
    try:
        requeued = _wanted_requeue_open()
    except Exception as e:
        requeued = 0
        print(f"[WARNING] wanted requeue at boot failed: {e}")
    threading.Thread(target=_wanted_worker, daemon=True, name="wanted-worker").start()
    print("Hardcover wanted list: enabled"
          + (", auto-download ON (M4B-only)" if WANTED_AUTO_DOWNLOAD else ", dashboard only")
          + (f"; requeued {requeued} open books for a fresh sweep" if requeued else ""))

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
print(f"SMART_SORT (Gemini): {'Enabled (' + RANK_MODEL + ')' if GEMINI_API_KEY else 'Disabled'}"
      + (f", thinking_budget={RANK_THINKING_BUDGET}" if GEMINI_API_KEY and RANK_THINKING_BUDGET is not None else ""))
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
        'smart_prefetch': session.get('smart_prefetch', SMART_PREFETCH_DEFAULT),
        'abs_match_enabled': ABS_MATCH_ENABLED,
        'log_enabled': LOG_ENABLED,
        'wanted_enabled': WANTED_ENABLED,
        # Connection controls (Tor routing toggle + circuit renewal).
        'tor_available': _tor_available,
        'tor_renewable': _tor_available and _tor_managed,
        'tor_status': tor_status(),
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
    if mode == 'tor' and tor_status() == 'unavailable':
        return jsonify({'message': 'Tor is not available on this server.'}), 409
    session['route_mode'] = mode
    session.permanent = True  # remember the choice across browser restarts
    return jsonify({'mode': mode})


@app.route('/settings/prefetch', methods=['POST'])
def set_prefetch():
    """Persist this browser's smart-sort prefetch preference (on|off)."""
    data = request.get_json(silent=True) or {}
    mode = data.get('mode')
    if mode not in ('on', 'off'):
        return jsonify({'message': 'Invalid mode.'}), 400
    session['smart_prefetch'] = mode
    session.permanent = True
    return jsonify({'mode': mode})


@app.route('/api/connection')
def api_connection():
    """Lightweight poll target so the search page can wait for Tor to finish
    bootstrapping and enable itself the moment routing is usable."""
    return jsonify({
        'tor_status': tor_status(),
        'route_mode': current_route_mode(),
        'tor_available': _tor_available,
        'tor_renewable': _tor_available and _tor_managed,
    })


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


def _parse_abb_page(html):
    """Parse one ABB search-results page into a list of book dicts."""
    soup = BeautifulSoup(html, 'html.parser')
    posts = soup.select('.post')
    results = []
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
    return results


def _fetch_abb_page(sess, headers, query, page):
    """Fetch one ABB results page. Returns parsed books, [] when the page loads
    but has no posts, or None when the fetch itself failed -- callers that need
    to tell "nothing there" from "couldn't reach the mirror" rely on that."""
    url = f"https://{ABB_HOSTNAME}/page/{page}/?s={quote_plus(query)}"
    try:
        response = sess.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            print(f"[ERROR] Failed to fetch page {page}. Status Code: {response.status_code}")
            return None
        return _parse_abb_page(response.text)
    except Exception as e:
        print(f"[ERROR] Error fetching page {page}: {e}")
        return None


def search_audiobookbay(query, max_pages=5, sess=None):
    """Scrape ABB search results. Page 1 is fetched first (it answers "are there
    any results at all?" and most queries fit on it); the remaining pages are
    fetched CONCURRENTLY so total latency is ~2 round-trips instead of
    max_pages, and one stalled Tor stream can't serialize the rest. Results
    keep page order; pagination still stops at the first empty page.

    Returns None when the mirror couldn't be reached at all (page 1 failed) --
    distinct from [] meaning "reached it, nothing found". `sess` overrides the
    per-request route session (used by the background wanted worker)."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    sess = sess or scrape_session()
    results = _fetch_abb_page(sess, headers, query, 1)
    if results is None:
        return None
    if not results or max_pages < 2:
        return results
    with ThreadPoolExecutor(max_workers=max_pages - 1) as pool:
        pages = pool.map(lambda p: _fetch_abb_page(sess, headers, query, p),
                         range(2, max_pages + 1))
    for page_results in pages:
        if not page_results:  # a failed or empty later page just ends the run
            break
        results.extend(page_results)
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
    "Editions (standalone books):\n"
    "- When several candidates are the same standalone work (not part of a "
    "series block) -- different uploads of one book -- group them in 'editions': "
    "pick the best edition as best_id and list the rest as alt_ids, using the "
    "same quality rules as above and an alt_note for what each differs on. "
    "Different works stay separate, and a book with only one upload needs no "
    "edition entry. Never put a book that is already in a series block here.\n"
    "- If nothing needs grouping, return an empty 'editions' list.\n"
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
        "editions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "best_id": {"type": "integer"},
                    "alt_ids": {"type": "array", "items": {"type": "integer"}},
                    "alt_note": {"type": "string"},
                },
                "required": ["best_id", "alt_ids"],
            },
        },
    },
    "required": ["ordering", "buckets", "ambiguous", "interpretations", "series", "editions"],
}


# Asked for only when ABS matching is on, so the model canonicalizes each public
# result into a clean identity the app can join to the library LOCALLY. No
# library data is ever put in the prompt.
RANK_CANONICALIZE_INSTRUCTION = (
    "\nCanonicalize (for local library matching):\n"
    "- Return a 'canonical' array of clean identities used to match results "
    "against the user's library. Include an entry ONLY for candidates you did "
    "NOT place in a 'series' block above -- i.e. standalone books, and lone "
    "series books with too few results to form a shelf. For books already in a "
    "series block we read the series and number straight from the shelf, so "
    "listing them here too is redundant work. For each included id give the "
    "work's canonical title, its primary author, and -- if it belongs to a "
    "series -- the series name and number (seq), using your knowledge to resolve "
    "variant/bare titles (e.g. a lone 'Dreadgod' is Cradle #11). You are NOT "
    "given the user's library and must not guess what they own."
)


def _rank_schema_with_canonical():
    """A deep copy of the base schema with the per-result 'canonical' block added
    and required, used only when ABS matching is on."""
    schema = json.loads(json.dumps(RANK_RESPONSE_SCHEMA))
    schema["properties"]["canonical"] = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "title": {"type": "string"},
                "author": {"type": "string"},
                "series": {"type": "string"},
                "seq": {"type": "number"},
            },
            "required": ["id"],
        },
    }
    schema["required"] = schema["required"] + ["canonical"]
    return schema


def rank_results(query, results, want_ownership=False):
    """Ask Gemini to re-rank already-scraped results. Returns the parsed JSON
    dict (ordering / buckets / ambiguous / interpretations). Raises on any
    transport or parsing failure so the caller can fall back to the existing
    order. When want_ownership is set, also asks for a per-result 'canonical'
    identity (for local library matching); the base behaviour is untouched
    otherwise."""
    global _thinking_supported
    cache_key = _rank_cache_key(query, results, want_ownership)
    cached = _rank_cache_get(cache_key)
    if cached is not None:
        print(f"[SMART SORT] cache hit for {query!r} ({len(results)} results)")
        return json.loads(cached)

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    system_instruction = RANK_SYSTEM_INSTRUCTION
    if PREFERRED_LANGUAGE:
        system_instruction += (
            f"\nLanguage: the user listens in {PREFERRED_LANGUAGE}. Rank editions "
            f"in other languages far below {PREFERRED_LANGUAGE} ones, bucket "
            "clearly wrong-language items as 'unlikely', and never pick a "
            "wrong-language upload as a series/edition best_id when a "
            f"{PREFERRED_LANGUAGE} one exists."
        )
    schema = RANK_RESPONSE_SCHEMA
    if want_ownership:
        system_instruction += RANK_CANONICALIZE_INSTRUCTION
        schema = _rank_schema_with_canonical()
    prompt = (
        f"User search query: {query}\n\n"
        f"Candidates (JSON):\n{json.dumps(results, ensure_ascii=False)}"
    )
    config_kwargs = dict(
        system_instruction=system_instruction,
        response_mime_type="application/json",
        response_schema=schema,
        temperature=0,
    )
    if RANK_THINKING_BUDGET is not None and _thinking_supported:
        config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=RANK_THINKING_BUDGET)

    def _generate(cfg):
        return client.models.generate_content(
            model=RANK_MODEL, contents=prompt,
            config=types.GenerateContentConfig(**cfg),
        )

    started = time.monotonic()
    try:
        response = _generate(config_kwargs)
    except Exception as e:
        # A thinking budget the model doesn't support shouldn't break sort --
        # retry once without it, and stop sending it so we don't keep paying a
        # failed call on every rank.
        if "thinking_config" not in config_kwargs:
            raise
        _thinking_supported = False
        print(f"[WARNING] Ranking with thinking_budget={RANK_THINKING_BUDGET} failed ({e}); retrying without it.")
        config_kwargs.pop("thinking_config")
        response = _generate(config_kwargs)
    print(f"[SMART SORT] {RANK_MODEL} thinking={RANK_THINKING_BUDGET} "
          f"{len(results)} results in {time.monotonic() - started:.1f}s")
    ranking = json.loads(response.text)  # validate before caching
    _rank_cache_put(cache_key, response.text)
    return ranking


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
        ranking = rank_results(query, results, want_ownership=ABS_MATCH_ENABLED)
        if ABS_MATCH_ENABLED:
            resolve_ownership(ranking, get_abs_index(), results)
        return jsonify(ranking)
    except Exception as e:
        print(f"[ERROR] Smart sort failed: {e}")
        return jsonify({'message': f'Smart sort failed: {e}'}), 502


@app.route('/api/ownership', methods=['POST'])
def api_ownership():
    """Re-check ownership of results already on the page against a freshly-ish
    ABS index, so a book that finished downloading into Audiobookshelf can flip
    to 'in your library' in place -- no re-search, no LLM. The client sends only
    the identities of results it currently shows as un-owned (ids + title, and,
    when it has them from a prior smart sort, author/series/seq)."""
    if not ABS_MATCH_ENABLED:
        return jsonify({'ownership': []})
    data = request.get_json(silent=True) or {}
    items = data.get('items') or []
    index = get_abs_index(max_age=120)  # at most one ABS fetch per ~2 min
    if not index:
        return jsonify({'ownership': []})
    owned_series = _owned_series_index(index)
    ownership = []
    for it in items:
        rid = it.get('id')
        if rid is None:
            continue
        # Without a canonical author (page not smart-sorted), split it out of the
        # raw ABB title so the matcher can author-gate, mirroring the initial pass.
        if not it.get('author') and not it.get('series'):
            title, author = abs_match.split_title_author(it.get('title', ''))
            it = {**it, 'title': title, 'author': author}
        if _canonical_owned(it, index, owned_series) is not None:
            ownership.append({'id': rid, 'status': 'owned'})
    return jsonify({'ownership': ownership})


# Endpoint for search page
@app.route('/', methods=['GET', 'POST'])
def search():
    books = []
    # A search runs on the POSTed form, or on a shareable GET link (/?q=...) --
    # the Upgrade Radar uses the latter to deep-link straight into results.
    query = (request.form.get('query', '') if request.method == 'POST'
             else request.args.get('q', '')).strip().lower()
    if query:
        # If this browser is set to Tor but Tor is still bootstrapping, don't
        # silently scrape over Direct -- tell the client to wait (it polls and
        # re-enables) or switch to Direct.
        if current_route_mode() == 'tor' and tor_status() != 'ready':
            if request.method == 'POST':
                return jsonify({'message': 'Tor is still starting. Please wait a moment or switch to Direct.',
                                'tor_status': tor_status()}), 503
            query, books = '', []  # GET deep link while Tor boots: render the page unsearched
        else:
            books = search_audiobookbay(query) or []  # None = mirror unreachable
            # Float preferred results to the top: matching-language first (when a
            # PREFERRED_LANGUAGE is set), then M4B. Python's stable sort keeps the
            # mirror's original ordering within each group.
            books.sort(key=lambda b: (not _language_matches(b), not b.get('is_m4b')))
    # Tag each result with a stable id so the client can ask Gemini to re-rank
    # them and then reorder the matching cards in place (see /api/rank).
    for i, book in enumerate(books):
        book['id'] = i
    annotate_library_matches(books)
    m4b_count = sum(1 for b in books if b.get('is_m4b'))
    owned_count = sum(1 for b in books if b.get('library_match'))
    return render_template('search.html', books=books, query=query,
                           searched=bool(query),
                           result_count=len(books), m4b_count=m4b_count,
                           owned_count=owned_count,
                           rank_payload=rank_payload(books))


@app.route('/upgrades')
def upgrades():
    """Upgrade Radar: scan the Audiobookshelf index for below-par copies (low
    effective bitrate, per-chapter MP3 rips) and offer a one-click ABB search
    for each. Everything is local arithmetic on the cached index -- see
    _quality_flag. Worst copies first."""
    if not ABS_MATCH_ENABLED:
        return render_template('upgrades.html', enabled=False, flagged=[],
                               total=0, low_kbps=ABS_LOW_KBPS)
    index = get_abs_index()
    flagged = []
    for item in index:
        reason = _quality_flag(item)
        if not reason:
            continue
        series = next(iter(item.get("series") or []), None)
        size, duration = item.get("size") or 0, item.get("duration") or 0
        flagged.append({
            "title": item["title"],
            "author": item["author"],
            "series": f"{series[0]} #{_norm_seq(series[1])}" if series and series[0] and series[1] is not None else "",
            "kbps": item.get("est_kbps"),
            "tracks": item.get("tracks") or 0,
            "size_h": f"{size / 1048576:.0f} MB" if size else "?",
            "duration_h": f"{duration / 3600:.1f} h" if duration else "?",
            "reason": reason,
            # Deep link into search; title + author is the query most likely to
            # surface a clean M4B of the same book.
            "query": " ".join(x for x in (item["title"], item["author"].split(",")[0].strip()) if x),
        })
    flagged.sort(key=lambda f: (f["kbps"] is None, f["kbps"] or 0.0, -f["tracks"]))
    return render_template('upgrades.html', enabled=True, flagged=flagged,
                           total=len(index), low_kbps=ABS_LOW_KBPS)


@app.route('/wanted')
def wanted():
    """Hardcover wanted-list dashboard: every 'Want to Read' book with its
    pipeline status and, when found, the best ABB match ready to send."""
    if not WANTED_ENABLED:
        return render_template('wanted.html', enabled=False, rows=[], counts={},
                               auto=WANTED_AUTO_DOWNLOAD, sync_error="")
    rows = sorted(_wanted_rows(), key=lambda r: (r.get("title") or "").lower())
    order = {"found": 0, "wanted": 1, "unmatched": 2, "sent": 3, "owned": 4}
    rows.sort(key=lambda r: order.get(r.get("status") or "wanted", 1))
    for r in rows:  # manual Search uses the same broad primary query as the worker
        r["search_q"] = _wanted_queries(r.get("title") or "", r.get("author") or "")[0]
        try:  # stored candidates beyond the pick become the expandable tray
            alts = json.loads(r.get("candidates") or "[]")[1:]
        except ValueError:
            alts = []
        for a in alts:
            a["meta"] = " · ".join(x for x in (a.get("format"), a.get("bitrate"),
                                               a.get("size"), a.get("language"))
                                   if x and x not in ("Unknown", "?"))
        r["alts"] = alts
    counts = {}
    for r in rows:
        counts[r.get("status") or "wanted"] = counts.get(r.get("status") or "wanted", 0) + 1
    return render_template('wanted.html', enabled=True, rows=rows, counts=counts,
                           auto=WANTED_AUTO_DOWNLOAD, sync_error=_wanted_sync_error,
                           route_label=wanted_route_label())


@app.route('/wanted/sync', methods=['POST'])
def wanted_sync():
    """Refresh the list from Hardcover now and make every open row due for a
    re-search; the background worker drains them a few per minute."""
    if not WANTED_ENABLED:
        return jsonify({'message': 'Hardcover is not configured.'}), 503
    try:
        _wanted_sync_list()
    except Exception as e:
        return jsonify({'message': f'Hardcover sync failed: {e}'}), 502
    for row in _wanted_rows():
        if row.get("status") not in ("sent", "owned"):
            _wanted_upsert({"hc_id": row["hc_id"], "searched_at": None})
    return redirect(url_for('wanted'))


@app.route('/wanted/research/<int:hc_id>', methods=['POST'])
def wanted_research(hc_id):
    """Re-search one wanted book right now (synchronous -- it's one scrape)."""
    if not WANTED_ENABLED:
        return jsonify({'message': 'Hardcover is not configured.'}), 503
    row = next((r for r in _wanted_rows() if r["hc_id"] == hc_id), None)
    if not row:
        return jsonify({'message': 'Unknown wanted book.'}), 404
    _wanted_search_one(row)
    return redirect(url_for('wanted'))


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
    if not _is_abb_link(details_url):
        record_download(user, title, details_url, None, 'error', 'Rejected: not an AudiobookBay link')
        return jsonify({'message': 'Only AudiobookBay links can be sent.'}), 400
    if not CLIENT_OK:
        return jsonify({'message': CLIENT_CONFIG_ERROR}), 503

    try:
        magnet_link = extract_magnet_link(details_url)
        if not magnet_link:
            record_download(user, title, details_url, None, 'error', 'Failed to extract magnet link')
            return jsonify({'message': 'Failed to extract magnet link'}), 500

        DOWNLOAD_BACKENDS[DOWNLOAD_CLIENT]['add'](magnet_link, title)
        record_download(user, title, details_url, _infohash_from_magnet(magnet_link), 'ok')
        _wanted_mark_sent_by_link(details_url)
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
        if not _is_abb_link(link):
            record_download(user, title, link, None, 'error',
                            'Rejected: not an AudiobookBay link', batch_id, batch_label)
            results.append({'link': link, 'title': title, 'ok': False, 'error': 'Not an AudiobookBay link'})
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
            _wanted_mark_sent_by_link(link)
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
init_wanted()  # after outbound: the worker scrapes through the Tor/direct sessions


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5078)
