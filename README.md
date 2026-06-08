
# AudiobookBay Automated

AudiobookBay Automated is a lightweight web application designed to simplify audiobook management. It allows users to search [**AudioBook Bay**](https://audiobookbay.lu/) for audiobooks and send magnet links directly to a designated **Deludge, qBittorrent or Transmission** client.

## How It Works
- **Search Results**: Users search for audiobooks. The app grabs results from AudioBook Bay and displays results with the **title** and **cover image**, along with two action links:
  1. **More Details**: Opens the audiobook's page on AudioBook Bay for additional information.
  2. **Download to Server**: Sends the audiobook to your configured torrent client for downloading.

- **Magnet Link Generation**: When a user selects "Download to Server," the app generates a magnet link from the infohash displayed on AudioBook Bay and sends it to the torrent client. Along with the magnet link, the app assigns:
  - A **category label** for organizational purposes.
  - A **save location** for downloaded files.


> **Note**: This app does not download or move any material itself (including torrent files). It only searches AudioBook Bay and facilitates magnet link generation for torrent.


## Features
- **Search Audiobook Bay**: Easily search for audiobooks by title or keywords.
- **View Details**: Displays book titles and covers with quickly links to the full details on AudioBook Bay.
- **Basic Download Status Page**: Monitor the download status of items in your torrent client that share the specified category assigned.
- **No AudioBook Bay Account Needed**: The app automatically generates magnet links from the displayed infohashes and push them to your torrent client for downloading.
- **Automatic Folder Organization**: Once the download is complete, torrent will automatically move the downloaded audiobook files to your save location. Audiobooks are organized into subfolders named after the AudioBook Bay title, making it easy for [**Audiobookshelf**](https://www.audiobookshelf.org/) to automatically add completed downloads to its library.



## Why Use This?
AudiobookBay Downloader provides a simple and user-friendly interface for users to download audiobooks without on their own and import them into your libary. 

---

## Installation

### Prerequisites
- **Deluge, qBittorrent, Transmission, or Put.io** (with WebUI enabled for local clients)
- **Docker** (optional, for containerized deployments)

### Environment Variables
The app uses environment variables to configure its behavior. Below are the required variables:

**For qBittorrent, Transmission, or Deluge:**
```env
DOWNLOAD_CLIENT=qbittorrent    # or transmission, delugeweb
DL_SCHEME=http
DL_HOST=192.168.xxx.xxx        # IP or hostname of your torrent client
DL_PORT=8080                   # torrent WebUI port
DL_USERNAME=YOUR_USER          # torrent username
DL_PASSWORD=YOUR_PASSWORD      # torrent password
DL_CATEGORY=abb-downloader     # torrent category for downloads
SAVE_PATH_BASE=/audiobooks     # Root path for audiobook downloads (relative to torrent)
ABB_HOSTNAME='audiobookbay.is' #Default
```

**For Put.io:**

Put.io supports two ways to authenticate — pick **one**:

**Option A — Log in with Put.io (OAuth).** Register an OAuth app on Put.io and let
users sign in from the app via the "Log in with Put.io" button. The token is kept
in the user's session.
```env
DOWNLOAD_CLIENT=putio
PUTIO_CLIENT_ID=YOUR_CLIENT_ID          # from your Put.io OAuth app
PUTIO_CLIENT_SECRET=YOUR_CLIENT_SECRET  # from your Put.io OAuth app
PUTIO_SAVE_PARENT_ID=0                  # Optional: Folder ID to save downloads (0 for root)
ABB_HOSTNAME='audiobookbay.is'          # Default
```

**Option B — Static token.** Skip the login flow and use an application-specific
token directly.
```env
DOWNLOAD_CLIENT=putio
PUTIO_ACCESS_TOKEN=YOUR_TOKEN  # application-specific token from put.io
PUTIO_SAVE_PARENT_ID=0         # Optional: Folder ID to save downloads (0 for root)
ABB_HOSTNAME='audiobookbay.is' #Default
```

If both are set, the OAuth session token (Option A) takes precedence.

#### Setting up Put.io OAuth (Option A)
1. Log in to your Put.io account
2. Go to Settings → Account → API / "Your OAuth Apps"
3. Create a new application (or use an existing one)
4. Set its **Callback URL** to `http(s)://<your-host>/putio/callback`
5. Copy the **Client ID** and **Client secret** into `PUTIO_CLIENT_ID` / `PUTIO_CLIENT_SECRET`

#### Getting a Put.io Access Token (Option B)
1. Log in to your Put.io account
2. Go to Settings → Account → API / "Your OAuth Apps"
3. Create a new application or use an existing one
4. Copy the **OAuth Token** and use it as `PUTIO_ACCESS_TOKEN`

The following optional variables add an additional entry to the navigation bar. This is useful for linking to your audiobook player or another related service:

```
NAV_LINK_NAME=Open Audiobook Player
NAV_LINK_URL=https://audiobooks.yourdomain.com/
```

#### Tor

Requests to AudiobookBay (search and magnet-link lookups) are routed through Tor
by default, so the mirror only ever sees a Tor exit node rather than your
server's real IP. The app starts and manages its own Tor process on startup —
nothing extra needs to be running, and the Docker image bundles the `tor` binary.
Requests to your download client and Put.io are **not** proxied.

These variables are all optional:

```env
USE_TOR=true                # Set to false to send AudiobookBay requests directly
TOR_AUTOSTART=true          # Set to false to use an already-running Tor instead
TOR_SOCKS_PORT=9050         # SOCKS port the app starts Tor on / connects to
TOR_BOOTSTRAP_TIMEOUT=90    # Seconds to wait for Tor to connect before failing
```

> Note: on first startup the app waits for Tor to bootstrap (usually a few
> seconds) before serving requests. If you run outside Docker, the `tor` binary
> must be installed and on your `PATH`.

### Using Docker

1. Use `docker-compose` for quick deployment. Example `docker-compose.yml`:

   **For qBittorrent/Transmission/Deluge:**
   ```yaml
   version: '3.8'

   services:
     audiobookbay-downloader:
       image: ghcr.io/jamesry96/audiobookbay-automated:latest
       ports:
         - "5078:5078"
       container_name: audiobookbay-downloader
       environment:
         - DOWNLOAD_CLIENT=qbittorrent
         - DL_SCHEME=http
         - DL_HOST=192.168.1.123
         - DL_PORT=8080
         - DL_USERNAME=admin
         - DL_PASSWORD=pass
         - DL_CATEGORY=abb-downloader
         - SAVE_PATH_BASE=/audiobooks
         - ABB_HOSTNAME=audiobookbay.is
         - NAV_LINK_NAME=Open Audiobook Player #Optional
         - NAV_LINK_URL=https://audiobooks.yourdomain.com/ #Optional
   ```

   **For Put.io:**
   ```yaml
   version: '3.8'

   services:
     audiobookbay-downloader:
       image: ghcr.io/jamesry96/audiobookbay-automated:latest
       ports:
         - "5078:5078"
       container_name: audiobookbay-downloader
       environment:
         - DOWNLOAD_CLIENT=putio
         - PUTIO_ACCESS_TOKEN=YOUR_PUTIO_TOKEN
         - PUTIO_SAVE_PARENT_ID=0
         - ABB_HOSTNAME=audiobookbay.is
         - NAV_LINK_NAME=Open Audiobook Player #Optional
         - NAV_LINK_URL=https://audiobooks.yourdomain.com/ #Optional
   ```

2. **Start the Application**:
   ```bash
   docker-compose up -d
   ```

### Running Locally
1. **Install Dependencies**:
   Ensure you have Python installed, then install the required dependencies:
   ```bash
   pip install -r requirements.txt
   
2. Create a .env file in the project directory to configure your application. Below are examples:

    **For qBittorrent/Transmission/Deluge:**
    ```
    # Torrent Client Configuration
    DOWNLOAD_CLIENT=qbittorrent # Change to delugeweb, transmission, or putio
    DL_SCHEME=http
    DL_HOST=192.168.1.123
    DL_PORT=8080
    DL_USERNAME=admin
    DL_PASSWORD=pass
    DL_CATEGORY=abb-downloader
    SAVE_PATH_BASE=/audiobooks
    
    # AudiobookBay Hostname
    ABB_HOSTNAME=audiobookbay.is

    # Optional Navigation Bar Entry
    NAV_LINK_NAME=Open Audiobook Player
    NAV_LINK_URL=https://audiobooks.yourdomain.com/
    ```

    **For Put.io:**
    ```
    # Put.io Configuration
    DOWNLOAD_CLIENT=putio
    PUTIO_ACCESS_TOKEN=YOUR_PUTIO_TOKEN
    PUTIO_SAVE_PARENT_ID=0
    
    # AudiobookBay Hostname
    ABB_HOSTNAME=audiobookbay.is

    # Optional Navigation Bar Entry
    NAV_LINK_NAME=Open Audiobook Player
    NAV_LINK_URL=https://audiobooks.yourdomain.com/
    ```

3. Start the app:
   ```bash
   python app.py
   ```

---

## Notes
- **This app does NOT download any material**: It simply generates magnet links and sends them to your qBittorrent client for handling.

- **Folder Mapping**: __The `SAVE_PATH_BASE` is based on the perspective of your torrent client__, not this app. This app does not move any files; all file handling and organization are managed by the torrent client. Ensure that the `SAVE_PATH_BASE` in your torrent client aligns with your audiobook library (e.g., for Audiobookshelf). Using a path relative to where this app is running, instead of the torrent client, will cause issues.


---

## Feedback and Contributions
This project is a work in progress, and your feedback is welcome! Feel free to open issues or contribute by submitting pull requests.

---

## Screenshots
### Search Results
![screenshot-2025-01-13-19-59-03](https://github.com/user-attachments/assets/8a30fd4e-a289-49d0-83ab-67a3bcfc9745)

### Download Status
![screenshot-2025-01-13-19-59-25](https://github.com/user-attachments/assets/19cc74de-51fc-422f-9cab-fe69e30c74b9)

---
