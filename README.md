# AudiobookBay Automated

A lightweight self-hosted web app for finding audiobooks on
[**AudioBook Bay**](https://audiobookbay.lu/) and sending them straight to your
[**Put.io**](https://put.io/) account — with the AudioBook Bay traffic routed
through Tor, M4B results floated to the top, and an optional Gemini-powered
"Smart sort" that cleans up AudioBook Bay's noisy search ordering.

Once a download finishes on Put.io, the files are ready for a library manager
like [**Audiobookshelf**](https://www.audiobookshelf.org/) to pick up.

> **This app does not download, store, or move any audiobook files itself.** It
> searches AudioBook Bay, builds magnet links from the infohashes shown there,
> and hands them to Put.io. All downloading and storage happen on Put.io.

---

## Features

- **Search AudioBook Bay** by title, author, or keywords, with covers and
  metadata (format, bitrate, size, language) shown for each result.
- **Tor by default** — all AudioBook Bay requests go through a Tor circuit, so
  the mirror only ever sees a Tor exit node, never your server's IP. The app
  starts and manages its own Tor process; nothing else needs to be running.
- **M4B prioritized** — M4B results (single-file audiobooks with chapters) are
  highlighted and floated to the top automatically.
- **Smart sort (optional)** — re-rank a noisy result set with Google Gemini,
  filtering out clearly-irrelevant hits and disambiguating vague queries. See
  [Smart sort](#smart-sort-gemini) below.
- **Send to Put.io** in one click — the app generates the magnet link and adds
  it as a Put.io transfer, optionally into a specific folder.
- **Download status page** — monitor your active Put.io transfers and their
  progress from within the app.
- **No AudioBook Bay account needed** — magnet links are built from the public
  infohashes on each listing.

---

## Installation

### Prerequisites

- A **Put.io** account (and either an OAuth app or an access token — see below).
- **Docker** (recommended) or Python 3.10+ to run it directly.
- *(Optional)* A **Google Gemini API key** if you want the Smart sort feature.

### Connecting Put.io

Put.io supports two ways to authenticate — pick **one**.

**Option A — Log in with Put.io (OAuth).** Register an OAuth app on Put.io and
sign in from the app via the "Log in with Put.io" button. The token is stored in
your browser session.

```env
PUTIO_CLIENT_ID=YOUR_CLIENT_ID          # from your Put.io OAuth app
PUTIO_CLIENT_SECRET=YOUR_CLIENT_SECRET  # from your Put.io OAuth app
PUTIO_SAVE_PARENT_ID=0                  # Optional: folder ID to save into (0 = root)
```

Set up the OAuth app:
1. Log in to Put.io → **Settings → Account → API / "Your OAuth Apps"**.
2. Create a new application (or reuse one).
3. Set its **Callback URL** to `http(s)://<your-host>/putio/callback`.
4. Copy the **Client ID** and **Client secret** into the variables above.

**Option B — Static token.** Skip the login flow and use an application-specific
token directly.

```env
PUTIO_ACCESS_TOKEN=YOUR_TOKEN  # application-specific token from Put.io
PUTIO_SAVE_PARENT_ID=0         # Optional: folder ID to save into (0 = root)
```

Get a token: Put.io → **Settings → Account → API / "Your OAuth Apps"** → create
an application → copy its **OAuth Token**.

> If both are configured, the OAuth session token (Option A) takes precedence.

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
The feature is hidden entirely unless `GEMINI_API_KEY` is set.

```env
GEMINI_API_KEY=your-google-ai-studio-key   # Enables Smart sort when set
RANK_MODEL=gemini-3.5-flash                 # Optional; Gemini model to use
```

> **Privacy note:** unlike AudioBook Bay scraping, the Smart sort request goes
> **directly to Google's API and is _not_ routed through Tor**. Only your search
> query and minimal result metadata (title, format, bitrate, language, size,
> keywords) are sent — never links, covers, or the mirror hostname. Leave
> `GEMINI_API_KEY` unset if you'd rather nothing leaves your server.

### Tor

AudioBook Bay requests (search and magnet-link lookups) are routed through Tor by
default. The app launches and manages its own Tor process on startup — nothing
extra needs to be running, and the Docker image bundles the `tor` binary.
Requests to Put.io and Google are **not** proxied.

These variables are all optional:

```env
USE_TOR=true                # Set to false to send AudioBook Bay requests directly
TOR_AUTOSTART=true          # Set to false to use an already-running Tor instead
TOR_SOCKS_PORT=9050         # SOCKS port the app starts Tor on / connects to
TOR_BOOTSTRAP_TIMEOUT=90    # Seconds to wait for Tor to connect before failing
```

> On first startup the app waits for Tor to bootstrap (usually a few seconds)
> before serving requests. If you run outside Docker, the `tor` binary must be
> installed and on your `PATH`.

---

## Running with Docker

Example `docker-compose.yml`:

```yaml
version: '3.8'

services:
  audiobookbay-automated:
    image: ghcr.io/dhamma-dev/audiobookbay-automated:latest
    ports:
      - "5078:5078"
    container_name: audiobookbay-automated
    environment:
      # Put.io (Option B shown; use PUTIO_CLIENT_ID/SECRET for OAuth instead)
      - PUTIO_ACCESS_TOKEN=YOUR_PUTIO_TOKEN
      - PUTIO_SAVE_PARENT_ID=0
      # Optional
      - ABB_HOSTNAME=audiobookbay.lu
      - GEMINI_API_KEY=YOUR_GEMINI_KEY            # enables Smart sort
      - NAV_LINK_NAME=Open Audiobook Player
      - NAV_LINK_URL=https://audiobooks.yourdomain.com/
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
2. Create a `.env` file in the project directory:
   ```env
   # Put.io
   PUTIO_ACCESS_TOKEN=YOUR_PUTIO_TOKEN
   PUTIO_SAVE_PARENT_ID=0

   # Optional
   ABB_HOSTNAME=audiobookbay.lu
   GEMINI_API_KEY=YOUR_GEMINI_KEY
   NAV_LINK_NAME=Open Audiobook Player
   NAV_LINK_URL=https://audiobooks.yourdomain.com/
   ```
3. Start the app:
   ```bash
   cd app && python app.py
   ```

---

## Notes

- **This app does not download or move any files.** It generates magnet links
  and adds them as Put.io transfers; Put.io handles the rest.
- **Folder organization** is controlled by `PUTIO_SAVE_PARENT_ID` (the Put.io
  folder new transfers are added to). Point your library manager (e.g.
  Audiobookshelf) at that location to import finished audiobooks.

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
