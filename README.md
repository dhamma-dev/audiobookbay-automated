# AudiobookBay Automated

A lightweight self-hosted web app for finding audiobooks on
[**AudioBook Bay**](https://audiobookbay.lu/) and sending them straight to your
download client — with the AudioBook Bay traffic routed through Tor, M4B results
floated to the top, and an optional Gemini-powered "Smart sort" that cleans up
AudioBook Bay's noisy search ordering.

Supported download clients: **qBittorrent, Transmission, Deluge, and Put.io.**
Once a download finishes, the files are ready for a library manager like
[**Audiobookshelf**](https://www.audiobookshelf.org/) to pick up.

> **This app does not download, store, or move any audiobook files itself.** It
> searches AudioBook Bay, builds magnet links from the infohashes shown there,
> and hands them to your chosen client. All downloading and storage happen there.

---

## Features

- **Search AudioBook Bay** by title, author, or keywords, with covers and
  metadata (format, bitrate, size, language) shown for each result.
- **Tor by default, with per-user controls** — all AudioBook Bay requests go
  through a Tor circuit, so the mirror only ever sees a Tor exit node, never your
  server's IP. The app starts and manages its own Tor process; a navbar menu lets
  each visitor toggle Tor ⇄ Direct and request a fresh exit IP on demand.
- **M4B prioritized** — M4B results (single-file audiobooks with chapters) are
  highlighted and floated to the top automatically.
- **Smart sort (optional)** — re-rank a noisy result set with Google Gemini:
  filter out clearly-irrelevant hits, disambiguate vague queries, and group a
  detected series into an ordered shelf you can send in one batch. See
  [Smart sort](#smart-sort-gemini).
- **Send in one click** to qBittorrent, Transmission, Deluge, or Put.io.
- **Download status page** — monitor active transfers and their progress from
  within the app.
- **Download log (for shared instances)** — records who added which book and
  when, reading the username from your reverse proxy's auth headers (e.g.
  Authentik). See [Download log](#download-log).
- **No AudioBook Bay account needed** — magnet links are built from the public
  infohashes on each listing.

---

## Installation

### Prerequisites

- One supported **download client**: qBittorrent, Transmission, or Deluge (each
  with its WebUI/RPC enabled), or a **Put.io** account.
- **Docker** (recommended) or Python 3.10+ to run it directly.
- *(Optional)* A **Google Gemini API key** if you want the Smart sort feature.

### Choosing your download client

The client is chosen at deploy time with `DOWNLOAD_CLIENT` — run one instance per
client. **`DOWNLOAD_CLIENT` is required**: if it's unset, unknown, or missing the
settings its client needs, the app shows a clear error banner and refuses to send
until it's fixed.

```env
DOWNLOAD_CLIENT=qbittorrent   # qbittorrent | transmission | delugeweb | putio
```

#### qBittorrent / Transmission / Deluge

These connect to a torrent client running on your network.

```env
DOWNLOAD_CLIENT=qbittorrent    # or: transmission, delugeweb
DL_SCHEME=http                 # http or https (Transmission/Deluge URL scheme)
DL_HOST=192.168.1.123          # IP or hostname of your client
DL_PORT=8080                   # WebUI / RPC port
DL_USERNAME=YOUR_USER          # client username (not needed for Deluge)
DL_PASSWORD=YOUR_PASSWORD      # client password
DL_CATEGORY=abb-downloader     # category/label applied to added torrents
SAVE_PATH_BASE=/audiobooks     # Optional: root save path (as the CLIENT sees it)
```

Required per client:

| Client        | `DOWNLOAD_CLIENT` | Required settings                                |
| ------------- | ----------------- | ------------------------------------------------ |
| qBittorrent   | `qbittorrent`     | `DL_HOST`, `DL_PORT`, `DL_USERNAME`, `DL_PASSWORD` |
| Transmission  | `transmission`    | `DL_HOST`, `DL_PORT`, `DL_USERNAME`, `DL_PASSWORD` |
| Deluge        | `delugeweb`       | `DL_HOST` + `DL_PORT` (or `DL_URL`), `DL_PASSWORD` |

> `SAVE_PATH_BASE` is interpreted **from the download client's perspective**, not
> this app's — point it at where your client should drop files (e.g. the folder
> Audiobookshelf watches). Leave it unset to use the client's own default. The
> app organizes downloads into per-title subfolders under it.

#### Put.io

Put.io is a cloud client and authenticates one of two ways — pick **one**.

**Option A — Log in with Put.io (OAuth).** Register an OAuth app on Put.io and
sign in from the app via the "Log in with Put.io" button. The token is stored in
your browser session.

```env
DOWNLOAD_CLIENT=putio
PUTIO_CLIENT_ID=YOUR_CLIENT_ID          # from your Put.io OAuth app
PUTIO_CLIENT_SECRET=YOUR_CLIENT_SECRET  # from your Put.io OAuth app
PUTIO_SAVE_PARENT_ID=0                  # Optional: folder ID to save into (0 = root)
```

Set up the OAuth app: Put.io → **Settings → Account → API / "Your OAuth Apps"** →
create an application → set its **Callback URL** to
`http(s)://<your-host>/putio/callback` → copy the **Client ID** and **Client
secret** into the variables above.

**Option B — Static token.** Skip the login flow and use an application-specific
token directly.

```env
DOWNLOAD_CLIENT=putio
PUTIO_ACCESS_TOKEN=YOUR_TOKEN  # application-specific token from Put.io
PUTIO_SAVE_PARENT_ID=0         # Optional: folder ID to save into (0 = root)
```

Get a token: Put.io → **Settings → Account → API / "Your OAuth Apps"** → create
an application → copy its **OAuth Token**. If both are configured, the OAuth
session token (Option A) takes precedence.

### Other configuration

All of the following are optional.

```env
ABB_HOSTNAME=audiobookbay.lu   # AudioBook Bay mirror to use (default shown)
REQUEST_TIMEOUT=45             # Hard cap (seconds) on outbound requests; unset = no cap

# Add an extra link to the navigation bar (e.g. your audiobook player)
NAV_LINK_NAME=Open Audiobook Player
NAV_LINK_URL=https://audiobooks.yourdomain.com/
```

### Smart sort (Gemini)

AudioBook Bay's own search is noisy and often floats irrelevant posts to the
top. When a Google Gemini API key is configured, a **Smart sort** button appears
above your results. It asks Gemini to re-rank the *already-loaded* results by how
well they match your query (preferring M4B on ties), hides clearly-irrelevant
ones behind a "Show N filtered results" toggle, and — when your query is
ambiguous — offers clickable chips to narrow to the interpretation you meant.
The feature is hidden entirely unless `GEMINI_API_KEY` is set, and works with any
download client.

**Series grouping.** When the results contain several books from one series,
Smart sort lays them out as an ordered shelf, with the best edition chosen per
book (preferring M4B and a healthy bitrate, and demoting low-bitrate, abridged,
or AI-narrated rips into a two-click *alternatives* tray you can swap from). Tick
the books you want and **send the whole set in one batch**, with each book logged
individually. A few niceties for big series:

- **Omnibus / box sets** are pinned to the top of the shelf; selecting one
  suppresses the individual books it covers so nothing is grabbed twice.
- **Gaps are obvious** — a book missing from the middle of a run shows as a
  subtle placeholder at its spot in reading order, and the shelf header reads
  e.g. "11 of 12 available" so you notice before sending.

Series grouping leans on Gemini's own knowledge of reading order and series
length; it only ever offers real listings to download, and gap markers are
informational, never something you can click.

```env
GEMINI_API_KEY=your-google-ai-studio-key   # Enables Smart sort when set
RANK_MODEL=gemini-3.5-flash                 # Optional; Gemini model to use
```

> **Privacy note:** unlike AudioBook Bay scraping, the Smart sort request goes
> **directly to Google's API and is _not_ routed through Tor**. Only your search
> query and minimal result metadata (title, format, bitrate, language, size,
> keywords) are sent — never links, covers, or the mirror hostname. Leave
> `GEMINI_API_KEY` unset if you'd rather nothing leaves your server.

### Download log

When you share an instance with others, the **Log** page records every send —
who added which book, when, over which route, and whether it succeeded. Books
sent together as a series batch are tagged as a set. It's backed by a small
SQLite file on the `./data` volume, so history survives restarts.

```env
LOG_DB_PATH=/data/downloads.db   # SQLite path; set empty to disable logging
LOG_ADMIN_USERS=alice,bob        # usernames who can see everyone's entries
```

`LOG_ADMIN_USERS` (comma-separated) may view all entries and filter by user;
anyone not listed sees only their own additions. Leave it unset to let every
user see the full log.

**Identity comes from your reverse proxy.** The log reads the username from
forwarded auth headers — `X-authentik-username` (Authentik), or `Remote-User` /
`X-Forwarded-User` from other forward-auth setups — falling back to the client
IP when none are present. For this to work your proxy must *forward* those
headers to the app (e.g. Authentik's `proxy_set_header X-authentik-username …`),
not just gate access.

> **Trust note:** the username is only as trustworthy as your proxy. If the app
> is reachable directly (its port published to the LAN), a client can bypass the
> proxy and forge the header. Put the app behind your proxy only — e.g. don't
> publish the container port and have the proxy reach it over a shared Docker
> network.

### Tor

AudioBook Bay requests (search and magnet-link lookups) are routed through Tor by
default, so the mirror only ever sees a Tor exit node rather than your server's
real IP. The app launches and manages its own Tor process on startup — nothing
extra needs to be running, and the Docker image bundles the `tor` binary.
Requests to your download client and to Google are **not** proxied.

**Per-user controls.** A **Connection** menu in the navbar lets each visitor:

- **Toggle Tor ⇄ Direct** for AudioBook Bay traffic. The choice is remembered in
  your browser. (Direct mode reveals your server's real IP to the mirror.)
- **Request a new Tor circuit** — if the current exit can't reach the mirror or
  is being blocked, this gets a fresh exit IP without a restart.

These variables are all optional:

```env
USE_TOR=true                # DEFAULT route for new visitors: true = Tor, false = Direct.
                            # Tor still runs either way so the toggle works; set
                            # TOR_AUTOSTART=false to not run Tor at all.
TOR_AUTOSTART=true          # Set to false to use an already-running Tor instead.
                            # Circuit renewal requires the app-managed Tor.
TOR_SOCKS_PORT=9050         # SOCKS port the app starts Tor on / connects to
TOR_CONTROL_PORT=9051       # Control port (localhost) used for circuit renewal
TOR_BOOTSTRAP_TIMEOUT=90    # Seconds to wait for Tor to connect before failing
```

> The app starts Tor in the background; if it can't (no `tor` binary, or
> `TOR_AUTOSTART=false` with nothing already listening) it runs in Direct-only
> mode and the toggle reflects that. If you run outside Docker, the `tor` binary
> must be installed and on your `PATH`.

---

## Running with Docker

Example `docker-compose.yml` (qBittorrent shown; swap the client block for
Transmission, Deluge, or Put.io as above):

```yaml
version: '3.8'

services:
  audiobookbay-automated:
    image: ghcr.io/dhamma-dev/audiobookbay-automated:latest
    ports:
      - "5078:5078"
    container_name: audiobookbay-automated
    volumes:
      - ./data:/data                             # persists the download log
    environment:
      - DOWNLOAD_CLIENT=qbittorrent
      - DL_SCHEME=http
      - DL_HOST=192.168.1.123
      - DL_PORT=8080
      - DL_USERNAME=admin
      - DL_PASSWORD=pass
      - DL_CATEGORY=abb-downloader
      - SAVE_PATH_BASE=/audiobooks
      # Optional
      - ABB_HOSTNAME=audiobookbay.lu
      - GEMINI_API_KEY=YOUR_GEMINI_KEY            # enables Smart sort
      - NAV_LINK_NAME=Open Audiobook Player
      - NAV_LINK_URL=https://audiobooks.yourdomain.com/
      - LOG_ADMIN_USERS=alice,bob                 # who can see the full download log
```

For **Put.io**, replace the `DL_*` / `SAVE_PATH_BASE` lines with:

```yaml
      - DOWNLOAD_CLIENT=putio
      - PUTIO_ACCESS_TOKEN=YOUR_PUTIO_TOKEN       # or PUTIO_CLIENT_ID/SECRET for OAuth
      - PUTIO_SAVE_PARENT_ID=0
```

```bash
docker-compose up -d
```

The app is then available on `http://<your-host>:5078`.

---

## Running locally

1. Install dependencies:
   ```bash
   pip install -r app/requirements.txt
   ```
2. Create a `.env` file in the project directory with `DOWNLOAD_CLIENT` and the
   matching settings from above (plus any optional Tor/Smart sort/nav vars).
3. Start the app:
   ```bash
   cd app && python app.py
   ```

---

## Notes

- **This app does not download or move any files.** It generates magnet links
  and hands them to your download client; the client handles the rest.
- **Folder organization** is controlled by `SAVE_PATH_BASE` (torrent clients, as
  the client sees the path) or `PUTIO_SAVE_PARENT_ID` (Put.io folder). Point your
  library manager at that location to import finished audiobooks.

---

## Feedback and Contributions

This project is a work in progress, and feedback is welcome. Feel free to open
issues or submit pull requests.

---

## Screenshots

### Search Results
![screenshot-2025-01-13-19-59-03](https://github.com/user-attachments/assets/8a30fd4e-a289-49d0-83ab-67a3bcfc9745)

### Download Status
![screenshot-2025-01-13-19-59-25](https://github.com/user-attachments/assets/19cc74de-51fc-422f-9cab-fe69e30c74b9)
