import os, re, requests, atexit, shutil, socket, subprocess, tempfile, threading
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
# Outbound requests to AudiobookBay are routed through Tor so the mirror only
# ever sees a Tor exit node, never the server's real IP. The app starts and
# manages its own Tor process, so nothing extra has to be running for this to
# work -- it all comes up with the app.
#
#   USE_TOR              - master switch (default on). Set to "false" to send
#                          AudiobookBay requests directly instead.
#   TOR_AUTOSTART        - let the app launch its own tor process (default on).
#                          Disable to point at an already-running Tor instead.
#   TOR_SOCKS_PORT       - SOCKS port to use / start Tor on (default 9050).
#   TOR_BOOTSTRAP_TIMEOUT- seconds to wait for Tor to connect (default 90).
def _is_truthy(value):
    return value.lower() not in ("0", "false", "no", "off", "")

USE_TOR = _is_truthy(os.getenv("USE_TOR", "true"))
TOR_AUTOSTART = _is_truthy(os.getenv("TOR_AUTOSTART", "true"))
TOR_SOCKS_PORT = int(os.getenv("TOR_SOCKS_PORT", "9050"))
TOR_BOOTSTRAP_TIMEOUT = int(os.getenv("TOR_BOOTSTRAP_TIMEOUT", "90"))

_tor_process = None
_tor_ready = threading.Event()


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
    """Launch a Tor process and block until it has bootstrapped. No-op if Tor is
    already listening on TOR_SOCKS_PORT."""
    global _tor_process

    if _socks_port_open(TOR_SOCKS_PORT):
        print(f"[TOR] Reusing Tor already listening on 127.0.0.1:{TOR_SOCKS_PORT}")
        return

    if not TOR_AUTOSTART:
        raise RuntimeError(
            f"USE_TOR is on but nothing is listening on 127.0.0.1:{TOR_SOCKS_PORT} "
            "and TOR_AUTOSTART is off."
        )

    tor_bin = shutil.which("tor")
    if not tor_bin:
        raise RuntimeError(
            "USE_TOR is on but the 'tor' binary was not found. Install Tor (the "
            "Docker image bundles it) or set USE_TOR=false to send requests directly."
        )

    data_dir = tempfile.mkdtemp(prefix="abb-tor-")
    print(f"[TOR] Starting Tor (SOCKS 127.0.0.1:{TOR_SOCKS_PORT})...")
    _tor_process = subprocess.Popen(
        [
            tor_bin,
            "--SocksPort", str(TOR_SOCKS_PORT),
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

    if not _tor_ready.wait(timeout=TOR_BOOTSTRAP_TIMEOUT):
        raise RuntimeError(f"Tor did not bootstrap within {TOR_BOOTSTRAP_TIMEOUT}s.")
    if _tor_process.poll() is not None:
        raise RuntimeError("Tor exited before it finished bootstrapping; see logs above.")
    print("[TOR] Tor is ready; AudiobookBay requests will be routed through it.")


def _build_session():
    """Build the requests Session used for AudiobookBay scraping. When Tor is
    enabled it proxies through Tor's SOCKS port; socks5h keeps DNS resolution on
    the Tor side too, so the hostname never leaks."""
    session = requests.Session()
    if USE_TOR:
        proxy = f"socks5h://127.0.0.1:{TOR_SOCKS_PORT}"
        session.proxies = {"http": proxy, "https": proxy}
    return session


def init_outbound():
    """Bring up Tor (if enabled) and build the scraping session. Called once at
    startup so everything is ready before the first request."""
    global SESSION
    if USE_TOR:
        _start_tor()
    SESSION = _build_session()


SESSION = None

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

#Print configuration
print(f"ABB_HOSTNAME: {ABB_HOSTNAME}")
print(f"USE_TOR: {USE_TOR}" + (f" (SOCKS 127.0.0.1:{TOR_SOCKS_PORT})" if USE_TOR else ""))
print(f"DOWNLOAD_CLIENT: {DOWNLOAD_CLIENT}")
print(f"DL_HOST: {DL_HOST}")
print(f"DL_PORT: {DL_PORT}")
print(f"DL_URL: {DL_URL}")
print(f"DL_USERNAME: {DL_USERNAME}")
print(f"DL_CATEGORY: {DL_CATEGORY}")
print(f"SAVE_PATH_BASE: {SAVE_PATH_BASE}")
print(f"NAV_LINK_NAME: {NAV_LINK_NAME}")
print(f"NAV_LINK_URL: {NAV_LINK_URL}")
if DOWNLOAD_CLIENT == 'putio':
    print(f"PUTIO_CLIENT_ID: {'Set' if PUTIO_CLIENT_ID else 'Not Set'}")
    print(f"PUTIO_CLIENT_SECRET: {'Set' if PUTIO_CLIENT_SECRET else 'Not Set'}")
    print(f"PUTIO_ACCESS_TOKEN: {'Set' if PUTIO_ACCESS_TOKEN else 'Not Set'}")
    print(f"PUTIO_SAVE_PARENT_ID: {PUTIO_SAVE_PARENT_ID}")


CLIENT_LABELS = {
    'qbittorrent': 'qBittorrent',
    'transmission': 'Transmission',
    'delugeweb': 'Deluge',
    'putio': 'Put.io',
}


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
        # Prompt for auth only when put.io is selected but we have no usable token.
        'show_putio_banner': is_putio and not putio_authenticated,
        'putio_authenticated': putio_authenticated,
        'putio_oauth_available': putio_oauth_available,
        'putio_session_login': putio_session_login,
        'page_title_suffix': 'AudiobookBay',
    }

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
    results = []
    for page in range(1, max_pages + 1):
        url = f"https://{ABB_HOSTNAME}/page/{page}/?s={query.replace(' ', '+')}"
        try:
            response = SESSION.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
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
                    
                    results.append({
                        'title': title,
                        'link': link,
                        'cover': cover,
                        'size': size,
                        'format': format_info,
                        'bitrate': bitrate,
                        'language': language,
                        'keywords': keywords
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
        response = SESSION.get(details_url, headers=headers, timeout=REQUEST_TIMEOUT)
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

# Endpoint for search page
@app.route('/', methods=['GET', 'POST'])
def search():
    books = []
    query = ''
    if request.method == 'POST':
        query = request.form.get('query', '').strip().lower()
        if query:
            books = search_audiobookbay(query)
    return render_template('search.html', books=books, query=query, result_count=len(books))


# Endpoint to send magnet link to download client
@app.route('/send', methods=['POST'])
def send():
    data = request.json
    details_url = data.get('link')
    title = data.get('title')
    if not details_url or not title:
        return jsonify({'message': 'Invalid request'}), 400

    try:
        magnet_link = extract_magnet_link(details_url)
        if not magnet_link:
            return jsonify({'message': 'Failed to extract magnet link'}), 500

        save_path = f"{SAVE_PATH_BASE}/{sanitize_title(title)}"
        
        if DOWNLOAD_CLIENT == 'qbittorrent':
            qb = Client(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            qb.auth_log_in()
            qb.torrents_add(urls=magnet_link, save_path=save_path, category=DL_CATEGORY)
        elif DOWNLOAD_CLIENT == 'transmission':
            transmission = transmissionrpc(host=DL_HOST, port=DL_PORT, protocol=DL_SCHEME, username=DL_USERNAME, password=DL_PASSWORD)
            transmission.add_torrent(magnet_link, download_dir=save_path)
        elif DOWNLOAD_CLIENT == "delugeweb":
            delugeweb = delugewebclient(url=DL_URL, password=DL_PASSWORD)
            delugeweb.login()
            delugeweb.add_torrent_magnet(magnet_link, save_directory=save_path, label=DL_CATEGORY)
        elif DOWNLOAD_CLIENT == 'putio':
            if not get_putio_token():
                return jsonify({'message': 'Put.io is not connected. Log in with Put.io or set PUTIO_ACCESS_TOKEN.'}), 401
            send_to_putio(magnet_link, title)
        else:
            return jsonify({'message': 'Unsupported download client'}), 400

        return jsonify({'message': f'Download added successfully! This may take some time, the download will show in Audiobookshelf when completed.'})
    except Exception as e:
        return jsonify({'message': str(e)}), 500

def get_torrent_list():
    if DOWNLOAD_CLIENT == 'transmission':
        transmission = transmissionrpc(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
        torrents = transmission.get_torrents()
        return [
            {
                'name': torrent.name,
                'progress': round(torrent.progress, 2),
                'state': torrent.status,
                'size': f"{torrent.total_size / (1024 * 1024):.2f} MB"
            }
            for torrent in torrents
        ]
    if DOWNLOAD_CLIENT == 'qbittorrent':
        qb = Client(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
        qb.auth_log_in()
        torrents = qb.torrents_info(category=DL_CATEGORY)
        return [
            {
                'name': torrent.name,
                'progress': round(torrent.progress * 100, 2),
                'state': torrent.state,
                'size': f"{torrent.total_size / (1024 * 1024):.2f} MB"
            }
            for torrent in torrents
        ]
    if DOWNLOAD_CLIENT == 'delugeweb':
        delugeweb = delugewebclient(url=DL_URL, password=DL_PASSWORD)
        delugeweb.login()
        torrents = delugeweb.get_torrents_status(
            filter_dict={"label": DL_CATEGORY},
            keys=["name", "state", "progress", "total_size"],
        )
        return [
            {
                "name": torrent["name"],
                "progress": round(torrent["progress"], 2),
                "state": torrent["state"],
                "size": f"{torrent['total_size'] / (1024 * 1024):.2f} MB",
            }
            for k, torrent in torrents.result.items()
        ]
    if DOWNLOAD_CLIENT == 'putio':
        if not get_putio_token():
            raise ValueError('Put.io is not connected. Log in with Put.io or set PUTIO_ACCESS_TOKEN.')
        transfers = get_putio_transfers()
        return [
            {
                'name': transfer.get('name', 'Unknown'),
                'progress': transfer.get('percent_done', 0),
                'state': transfer.get('status', 'Unknown'),
                'size': f"{transfer.get('size', 0) / (1024 * 1024):.2f} MB"
            }
            for transfer in transfers
        ]
    raise ValueError('Unsupported download client')


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



# Bring up Tor and the scraping session before serving any requests. Done at
# import time so it also covers WSGI servers, not just `python app.py`.
init_outbound()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5078)
