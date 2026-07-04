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
- **"In your library" flagging (optional)** — results you already own in
  Audiobookshelf get a discreet badge, with a *Hide owned* toggle. Matched
  locally and precision-first. See [In your library](#in-your-library-audiobookshelf).
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
REQUEST_TIMEOUT=45             # Cap (seconds) on AudiobookBay fetches (default 45; 0/off = no cap)
PREFERRED_LANGUAGE=English     # Float this language's results up; unset = no preference

# Add an extra link to the navigation bar (e.g. your audiobook player)
NAV_LINK_NAME=Open Audiobook Player
NAV_LINK_URL=https://audiobooks.yourdomain.com/
```

> `PREFERRED_LANGUAGE` floats matching-language results above others in the
> normal result order, and (when Smart sort is enabled) tells Gemini to rank
> other-language editions far lower. Leave it unset to treat all languages
> equally. It's a plain-text match against each listing's language field, so use
> the word as the mirror shows it (e.g. `English`).

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

**Prepare automatically (prefetch).** Smart sort takes a few seconds. In the
settings popover (the shield menu) you can turn on **Prepare automatically**: it
runs smart sort in the background the moment results load, so the button shows
"Preparing…" and then "Apply smart sort" — instant when you click. Click while
it's still preparing and it applies the moment it's ready. This spends one Gemini
call per search, so it's opt-in per browser (`SMART_PREFETCH_DEFAULT` sets the
default for new visitors).

```env
GEMINI_API_KEY=your-google-ai-studio-key   # Enables Smart sort when set
RANK_MODEL=gemini-3.5-flash                 # Optional; Gemini model to use
SMART_PREFETCH_DEFAULT=off                  # Optional; "on" prefetches by default
RANK_THINKING_BUDGET=0                       # Default 0 (fastest); N allows thinking; negative = model default
```

> **Why the budget defaults to 0:** flash models spend time "thinking" before
> answering, and for a structured ranking task that's mostly wasted latency.
> Benchmarking showed **~4–5× faster** Smart sort at `RANK_THINKING_BUDGET=0`
> (e.g. ~49s → ~10s with library matching on) with no measurable quality loss
> and far less variance. Raise it to a positive token count if you ever notice
> worse series/ownership matching, or set a negative value to restore the
> model's own default thinking. The server logs `[SMART SORT] … in Xs` so you
> can compare, and if a model rejects the budget the app retries without it.

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

### In your library (Audiobookshelf)

When `ABS_URL` + `ABS_TOKEN` are set, search results that already exist in your
Audiobookshelf library get a discreet **"In your library"** badge, and a **Hide
owned** toggle appears so you can focus on what's new. Matching is done locally
and privately — the library is fetched once and cached in memory (`ABS_CACHE_TTL`
seconds), and nothing leaves your server.

```env
ABS_URL=https://audiobooks.yourdomain.com   # your Audiobookshelf base URL
ABS_TOKEN=your-abs-api-token                 # Settings → Users → (you) → API token
# ABS_LIBRARY_ID=...                         # optional; defaults to first book library
# ABS_CACHE_TTL=900                          # optional; library cache lifetime (seconds)
```

The matcher (`app/abs_match.py`) is **precision-first**: it only ever asserts a
positive, and only when confident. A strong title match is *gated on the author*
(so a same-title / wrong-author result is rejected), foreign-language editions
and bundles matched against a single owned volume are held back, and a match it
isn't sure about simply shows no badge. **A missing badge is never a claim you
_don't_ own something** — so a slight title variation can never mislead you. The
badge is informational and never blocks the Send button (you may still want a
different edition or narrator).

**With Smart sort, matching gets smarter — still without sending your library
anywhere.** When you run Smart sort, Gemini *canonicalizes the public search
results* (resolving variant or bare titles to their series and number — e.g. a
bare `Waybound` is Cradle #12), and the app joins those clean identities to your
library **locally, on the server**. That catches books the plain matcher misses,
adds a per-series **"own N of 12"** count, and flags bundles you only partly own
(**"Own 4 of 10"**). Only the public result metadata Smart sort already sends
leaves the box — never anything about what you own.

#### Upgrade radar

Owning a book isn't the end of the story — a 32 kbps rip split across 40 MP3s
is a candidate for replacement, not a reason to skip a clean M4B. When ABS is
connected, an **Upgrades** page appears in the nav: it computes every owned
copy's *effective bitrate* from its real size and duration (pure local
arithmetic — nothing leaves your server), flags copies at or below
`ABS_LOW_KBPS` (default 63) or fragmented per-chapter rips (≥ 8 files), and
gives each a **Find better** button that deep-links into a normal search
(`/?q=…`, which also makes searches shareable).

The loop closes in the results: a listing you own **well** shows the green
*In your library* badge, but a listing that would **beat a below-par copy**
(M4B, not worse than what you have) shows an amber **“Upgrade available ·
yours is ~48 kbps”** instead — and in series shelves, upgrades stay
**pre-selected** while owned-fine books stay unticked. Fill the gaps and
replace the junk in one send.

```env
ABS_LOW_KBPS=63   # Optional; flag owned copies at/below this effective bitrate
```

### Hardcover wanted list

Connect your [Hardcover](https://hardcover.app) account and your **“Want to
Read” list becomes a dashboard**: a **Wanted** page appears in the nav where
every wanted book is pre-searched on AudioBook Bay in the background and moves
through a pipeline — *Queued → Found → Sent → In your library* (books you
already own in Audiobookshelf are marked instead of searched).

When a search turns up results, they are **AI-rated once** (same Gemini model
as Smart sort): the model verifies each listing really is that exact work,
ranks the editions (M4B preferred, your language, healthy bitrate), flags red
flags like *abridged* or *AI-narrated*, and explains its pick in one line shown
on the row. The pick and all rated alternatives are then **persisted — found
books are settled** and never searched or rated again unless you force that
title with the per-row re-check (↻). Books with no confident match re-check
about once a day, so newly-uploaded books surface on their own.

```env
HARDCOVER_API_KEY=your-hardcover-token   # hardcover.app → account settings → Hardcover API
WANTED_AUTO_DOWNLOAD=false               # "true" auto-sends confident M4B matches
WANTED_ROUTE=default                     # background search route: default | tor | direct
```

> **Routing note:** background searches use the **server's default route**
> (`USE_TOR`), not your browser's Tor/Direct toggle — the toolbar shows which.
> Failed rows say so and retry within ~30 minutes, and the per-row re-check
> button always uses *your* browser's route, so it doubles as a diagnostic.
> **On Tor, the worker self-heals:** after 3 consecutive unreachable searches
> it automatically requests a fresh Tor circuit (at most once per 10 minutes —
> renewal swaps the exit for everyone on the instance) and immediately requeues
> the failed rows. Set `WANTED_ROUTE=direct` to pin background searches to
> Direct instead (trades the Tor shield for whatever reliability your direct
> path has).

With `WANTED_AUTO_DOWNLOAD=true` the app clears your wanted list for you:
when a background search finds a **confident match** (strict title+author
match, **M4B only**), it sends it to your download client automatically and
records it in the download log as `hardcover-auto`. Anything less than
confident stays a dashboard suggestion for you to decide.

> **Cost & privacy notes:** the AI verdict is **~one small call per wanted
> book, ever** — it fires only when a search first finds results, and the
> rating is persisted (found = settled). Set `WANTED_LLM=false` for a fully
> deterministic pipeline (M4B/language/bitrate rules, no calls at all). What
> the model sees is the wanted book's public title/author and the public ABB
> listing metadata — never your library. Hardcover is queried a couple of
> times per sync window (well under their 60 req/min limit), and ABB is
> searched through your normal Tor/Direct routing at most a few books per
> minute. Heads-up: Hardcover API tokens expire every January 1st, and their
> API is in beta.

#### Evaluating / tuning the matcher

`app/abs_match_spike.py` is a companion CLI (safe to delete) that prints how each
result of a real ABB search matches your library, so you can judge precision and
recall on your own data. It reuses the app's Tor and `.env`:

```bash
# offline logic check — no ABS or ABB needed, exercises the matcher + guards:
docker compose exec audiobookbay-automated python abs_match_spike.py --selftest

# live run against your library + real ABB searches:
docker compose exec audiobookbay-automated \
    python abs_match_spike.py "cradle" "land fit for heroes"
```

`STRONG` rows are what become a badge; `maybe`/`none` are shown only to gauge
recall. Thresholds live at the top of `app/abs_match.py`.

### Tor

AudioBook Bay requests (search and magnet-link lookups) are routed through Tor by
default, so the mirror only ever sees a Tor exit node rather than your server's
real IP. The app launches and manages its own Tor process on startup — nothing
extra needs to be running, and the Docker image bundles the `tor` binary.
Requests to your download client and to Google are **not** proxied.

Tor bootstraps **in the background**, so the app is reachable the instant it
starts rather than waiting for the circuit. If you open the page while Tor is
still connecting and your default route is Tor, search waits and enables itself
the moment Tor is ready — or you can switch to Direct and search immediately.
Defaulting to Direct (`USE_TOR=false`) lets you search right away regardless.

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

Working on the code? Start with [`CLAUDE.md`](CLAUDE.md) for an architecture
overview and conventions, with deep dives in [`docs/`](docs/).

---

## Screenshots

### Search results with series grouping
Smart sort lays a detected series out as an ordered shelf — best edition per
book, complete-set bundles pinned on top, and interpretation chips to narrow a
vague query.
![Search results with a Cradle series shelf and interpretation chips](https://github.com/user-attachments/assets/5a360066-1538-49cc-bac2-2e0b79c6721f)

### Download log
Who added which book, when, and over which route — for shared instances.
![Download log table](https://github.com/user-attachments/assets/d8b5ce15-1c3e-4482-b98b-3ed8a8a2cf0c)

### Per-user Tor routing
Toggle AudioBook Bay traffic between Tor and Direct, or request a fresh exit IP.
![Connection routing popover with a Tor toggle and new-circuit button](https://github.com/user-attachments/assets/e638324f-29b1-4766-814b-ddf0f93b07dd)
