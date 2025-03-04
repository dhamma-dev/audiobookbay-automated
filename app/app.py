import os, re, requests
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

# put.io OAuth credentials
PUTIO_CLIENT_ID = os.getenv("PUTIO_CLIENT_ID")
PUTIO_CLIENT_SECRET = os.getenv("PUTIO_CLIENT_SECRET")
PUTIO_REDIRECT_URI = os.getenv("PUTIO_REDIRECT_URI")
PUTIO_SAVE_PARENT_ID = os.getenv("PUTIO_SAVE_PARENT_ID")  # Default folder ID to save to

# Custom Nav Link Variables
NAV_LINK_NAME = os.getenv("NAV_LINK_NAME")
NAV_LINK_URL = os.getenv("NAV_LINK_URL")

#Print configuration
print(f"ABB_HOSTNAME: {ABB_HOSTNAME}")
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
    print(f"PUTIO_REDIRECT_URI: {PUTIO_REDIRECT_URI}")
    print(f"PUTIO_SAVE_PARENT_ID: {PUTIO_SAVE_PARENT_ID}")


@app.context_processor
def inject_nav_link():
    return {
        'nav_link_name': os.getenv('NAV_LINK_NAME'),
        'nav_link_url': os.getenv('NAV_LINK_URL')
    }

@app.context_processor
def inject_putio_auth_status():
    if DOWNLOAD_CLIENT == 'putio':
        return {
            'putio_authenticated': 'putio_access_token' in session,
            'putio_client_id': PUTIO_CLIENT_ID
        }
    return {
        'putio_authenticated': False,
        'putio_client_id': None
    }

# put.io OAuth routes
@app.route('/putio/auth')
def putio_auth():
    if DOWNLOAD_CLIENT != 'putio':
        return jsonify({'message': 'put.io is not configured as the download client'}), 400
    
    if not PUTIO_CLIENT_ID:
        return jsonify({'message': 'put.io client ID not configured'}), 400
    
    # Generate authorization URL
    auth_url = f"https://api.put.io/v2/oauth2/authenticate?client_id={PUTIO_CLIENT_ID}&response_type=code&redirect_uri={PUTIO_REDIRECT_URI}"
    return redirect(auth_url)

@app.route('/putio/callback')
def putio_callback():
    if DOWNLOAD_CLIENT != 'putio':
        return jsonify({'message': 'put.io is not configured as the download client'}), 400
    
    code = request.args.get('code')
    if not code:
        return jsonify({'message': 'Authorization code not received'}), 400
    
    # Exchange code for access token
    token_url = "https://api.put.io/v2/oauth2/access_token"
    data = {
        'client_id': PUTIO_CLIENT_ID,
        'client_secret': PUTIO_CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'redirect_uri': PUTIO_REDIRECT_URI,
        'code': code
    }
    
    response = requests.post(token_url, data=data)
    if response.status_code != 200:
        return jsonify({'message': f'Failed to get access token: {response.text}'}), 400
    
    token_data = response.json()
    session['putio_access_token'] = token_data.get('access_token')
    
    return redirect(url_for('search'))

def search_audiobookbay(query, max_pages=5):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    results = []
    for page in range(1, max_pages + 1):
        url = f"https://{ABB_HOSTNAME}/page/{page}/?s={query.replace(' ', '+')}&cat=undefined%2Cundefined"
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            print(f"[ERROR] Failed to fetch page {page}. Status Code: {response.status_code}")
            break
        
        soup = BeautifulSoup(response.text, 'html.parser')
        for post in soup.select('.post'):
            try:
                title = post.select_one('.postTitle > h2 > a').text.strip()
                link = f"https://{ABB_HOSTNAME}{post.select_one('.postTitle > h2 > a')['href']}"
                cover = post.select_one('img')['src'] if post.select_one('img') else "/static/images/default-cover.jpg"
                
                # Extract additional information from the post
                post_info = post.select_one('.postInfo')
                date_posted = post_info.select_one('.postDate').text.strip() if post_info.select_one('.postDate') else 'Unknown'
                
                # Extract categories, author, narrator if available
                categories = []
                author = 'Unknown'
                narrator = 'Unknown'
                size = 'Unknown'
                
                # Look for information in the post content
                post_content = post.select_one('.postContent')
                if post_content:
                    content_text = post_content.text.strip()
                    
                    # Try to find author and narrator from the content text
                    author_match = re.search(r'Author:\s*([^|]+)', content_text)
                    if author_match:
                        author = author_match.group(1).strip()
                    
                    narrator_match = re.search(r'Narrator:\s*([^|]+)', content_text)
                    if narrator_match:
                        narrator = narrator_match.group(1).strip()
                    
                    # Try to find size information
                    size_match = re.search(r'Size:\s*([\d.]+\s*[KMGT]B)', content_text)
                    if size_match:
                        size = size_match.group(1).strip()
                
                # Extract categories from tags if available
                category_tags = post.select('.postTags a')
                if category_tags:
                    categories = [tag.text.strip() for tag in category_tags]
                
                results.append({
                    'title': title,
                    'link': link, 
                    'cover': cover,
                    'date_posted': date_posted,
                    'author': author,
                    'narrator': narrator,
                    'size': size,
                    'categories': categories
                })
                
            except Exception as e:
                print(f"[ERROR] Skipping post due to error: {e}")
                continue
    
    return results

# # Helper function to search AudiobookBay
# def search_audiobookbay(query, max_pages=5):
#     headers = {
#         'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
#     }
#     results = []
#     for page in range(1, max_pages + 1):
#         url = f"https://{ABB_HOSTNAME}/page/{page}/?s={query.replace(' ', '+')}&cat=undefined%2Cundefined"
#         response = requests.get(url, headers=headers)
#         if response.status_code != 200:
#             print(f"[ERROR] Failed to fetch page {page}. Status Code: {response.status_code}")
#             break

#         soup = BeautifulSoup(response.text, 'html.parser')
#         for post in soup.select('.post'):
#             try:
#                 title = post.select_one('.postTitle > h2 > a').text.strip()
#                 link = f"https://{ABB_HOSTNAME}{post.select_one('.postTitle > h2 > a')['href']}"
#                 cover = post.select_one('img')['src'] if post.select_one('img') else "/static/images/default-cover.jpg"
#                 results.append({'title': title, 'link': link, 'cover': cover})
#             except Exception as e:
#                 print(f"[ERROR] Skipping post due to error: {e}")
#                 continue
#     return results

# Helper function to extract magnet link from details page
def extract_magnet_link(details_url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    try:
        response = requests.get(details_url, headers=headers)
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
    if 'putio_access_token' not in session:
        raise Exception("Not authenticated with put.io")
    
    api_url = "https://api.put.io/v2/transfers/add"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {session['putio_access_token']}"
    }
    
    data = {
        "url": magnet_link
    }
    
    # Add parent folder ID if specified
    if PUTIO_SAVE_PARENT_ID:
        data["save_parent_id"] = PUTIO_SAVE_PARENT_ID
    
    response = requests.post(api_url, data=data, headers=headers)
    
    if response.status_code != 200:
        raise Exception(f"Put.io API error: {response.text}")
    
    return response.json()

# Helper function to get put.io transfer status
def get_putio_transfers():
    """
    Gets the list of transfers from put.io
    """
    if 'putio_access_token' not in session:
        raise Exception("Not authenticated with put.io")
    
    api_url = "https://api.put.io/v2/transfers/list"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {session['putio_access_token']}"
    }
    
    response = requests.get(api_url, headers=headers)
    
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
    if request.method == 'POST':  # Form submitted
        query = request.form['query']
        #Convert to all lowercase
        query = query.lower()
        if query:  # Only search if the query is not empty
            books = search_audiobookbay(query)
    return render_template('search.html', books=books)


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
            if 'putio_access_token' not in session:
                return jsonify({'message': 'Not authenticated with put.io. Please login first.'}), 401
            send_to_putio(magnet_link, title)
        else:
            return jsonify({'message': 'Unsupported download client'}), 400

        return jsonify({'message': f'Download added successfully! This may take some time, the download will show in Audiobookshelf when completed.'})
    except Exception as e:
        return jsonify({'message': str(e)}), 500

@app.route('/status')
def status():
    try:
        if DOWNLOAD_CLIENT == 'transmission':
            transmission = transmissionrpc(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            torrents = transmission.get_torrents()
            torrent_list = [
                {
                    'name': torrent.name,
                    'progress': round(torrent.progress, 2),
                    'state': torrent.status,
                    'size': f"{torrent.total_size / (1024 * 1024):.2f} MB"
                }
                for torrent in torrents
            ]
            return render_template('status.html', torrents=torrent_list)
        elif DOWNLOAD_CLIENT == 'qbittorrent':
            qb = Client(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            qb.auth_log_in()
            torrents = qb.torrents_info(category=DL_CATEGORY)
            torrent_list = [
                {
                    'name': torrent.name,
                    'progress': round(torrent.progress * 100, 2),
                    'state': torrent.state,
                    'size': f"{torrent.total_size / (1024 * 1024):.2f} MB"
                }
                for torrent in torrents
            ]
        elif DOWNLOAD_CLIENT == "delugeweb":
            delugeweb = delugewebclient(url=DL_URL, password=DL_PASSWORD)
            delugeweb.login()
            torrents = delugeweb.get_torrents_status(
                filter_dict={"label": DL_CATEGORY},
                keys=["name", "state", "progress", "total_size"],
            )
            torrent_list = [
                {
                    "name": torrent["name"],
                    "progress": round(torrent["progress"], 2),
                    "state": torrent["state"],
                    "size": f"{torrent['total_size'] / (1024 * 1024):.2f} MB",
                }
                for k, torrent in torrents.result.items()
            ]
        elif DOWNLOAD_CLIENT == 'putio':
            if 'putio_access_token' not in session:
                return render_template('status.html', need_auth=True)
                
            transfers = get_putio_transfers()
            torrent_list = [
                {
                    'name': transfer.get('name', 'Unknown'),
                    'progress': transfer.get('percent_done', 0),
                    'state': transfer.get('status', 'Unknown'),
                    'size': f"{transfer.get('size', 0) / (1024 * 1024):.2f} MB"
                }
                for transfer in transfers
            ]
        else:
            return jsonify({'message': 'Unsupported download client'}), 400
        return render_template('status.html', torrents=torrent_list)
    except Exception as e:
        return jsonify({'message': f"Failed to fetch torrent status: {e}"}), 500



if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5078)
