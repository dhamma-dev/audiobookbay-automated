# CLAUDE.md

Orientation for AI sessions working in this repo. Keep this file lean — it loads
every session. Depth lives in [`docs/`](docs/); user-facing setup lives in
[`README.md`](README.md). When you change how something here works, update the
relevant doc in the same commit.

## What this is

A self-hosted **Flask** web app that searches [AudioBook Bay](https://audiobookbay.lu)
(an audiobook torrent index), and sends the chosen torrents to a download client
(qBittorrent / Transmission / Deluge / Put.io) so they land in the user's
**Audiobookshelf** library. It adds quality-of-life on top of raw ABB search:
M4B-preferred ranking, optional LLM "smart sort" (series grouping + relevance),
Tor routing for the ABB scrape, a shared-instance download log, and
"already in your Audiobookshelf library" flagging.

Single-user or small trusted group (family/friends behind Authentik).

## Shape of the code (v2)

The backend is a package (`app/abb/`) built by an **app factory** — importing
any module has **zero side effects**; `create_app()` (and only it) wires config,
services, and blueprints, and `create_app(start=False)` skips the side effects
entirely (what tests use). Frontend is server-rendered Jinja + one vanilla-JS
IIFE; no build step, no ORM. See [`docs/v2-review.md`](docs/v2-review.md) for
why v2 looks like this.

| Path | What it is |
|---|---|
| `app/main.py` | Entrypoint: `load_dotenv()` → `create_app()` → waitress (one process + threads — required by the caches/Tor/worker; never run multi-worker). |
| `app/abb/config.py` | Typed `Config.from_env()`: every env var, defaults, validation, masked startup report. |
| `app/abb/factory.py` | `create_app()` + the `Services` container (config/store/tor/outbound/scraper/library/rank/clients/wanted). |
| `app/abb/tor.py`, `outbound.py` | `TorManager` (launch/bootstrap/renew) and `Outbound` (direct+tor sessions, per-browser route mode). |
| `app/abb/scraper.py` | ABB search parse (pure `parse_search_page`) + magnet extraction. |
| `app/abb/storage.py` | SQLite (WAL): download log + wanted rows; v1 schema, self-migrating. |
| `app/abb/matching.py` | **Pure, stdlib-only** library matcher (v1 `abs_match.py`, unchanged logic). |
| `app/abb/library.py` | `AbsLibrary`: ABS index cache, ownership joins, Upgrade Radar. |
| `app/abb/smart_sort.py` | `RankService`: Gemini schemas/instructions, rank cache, wanted verdict. `RANK_FIELDS` is the privacy allowlist. |
| `app/abb/wanted.py` | `WantedService`: Hardcover sync, query ladder, worker thread, auto-download. |
| `app/abb/clients.py` | Download-client registry (add/list per client) + put.io token logic. |
| `app/abb/identity.py`, `security.py` | Proxy-header identity; CSRF, security headers, persisted secret key. |
| `app/abb/settings.py` | In-app settings overlay: `FEATURE_SETTINGS` overrides from SQLite, most-recently-set-wins vs env (snapshot comparison). UI in `web/admin.py`. |
| `app/abb/web/` | Blueprints: `pages` (HTML), `actions` (send/settings/wanted POSTs), `api` (JSON + `/healthz`), `putio` (OAuth). |
| `app/abb/templates/`, `static/` | Jinja + design-system CSS (`tokens.css` vars — always use these) + `js/app.js` (one IIFE, `data-action` delegation) + vendored icons (`icons.js` + `images/icons.svg` — **no CDN scripts**). |
| `app/tests/` | pytest suite; CI gates image builds on it. |
| `app/abs_match_spike.py` | Standalone matcher eval CLI; imports `abb.matching` only. |
| `Dockerfile`, `docker-compose.yaml` | python:3.12-slim + tor, non-root (uid 1000), `HEALTHCHECK` → `/healthz`. |
| `docs/` | Deep dives (architecture, library matching, development, v2 review). |

## Core subsystems (one line each — details in docs)

- **Download clients** — registry in `clients.py`; one client per deploy via `DOWNLOAD_CLIENT`. Adding a client = one backends entry + `CLIENT_REQUIRED_ENV`.
- **Tor routing** — the app starts/manages its own `tor`; ABB scrape + magnet lookup go through it, toggleable per browser (`session['route_mode']`). Everything else (Gemini, ABS, Hardcover, the download client) goes direct.
- **Smart sort** — optional Gemini call (`/api/rank`) re-ranking results, grouping series/editions. Only public result metadata (`RANK_FIELDS`) is sent. See [`docs/architecture.md`](docs/architecture.md).
- **ABS library matching** — deterministic baseline + LLM-canonicalized local join + live re-check poll. **Precision-first; the library never leaves the box.** See [`docs/library-matching.md`](docs/library-matching.md).
- **Download log** — SQLite audit of who sent what; identity from reverse-proxy auth headers (Authentik). `/log` gated by `LOG_ADMIN_USERS`.
- **Hardcover wanted list** — `/wanted` dashboard syncs "Want to Read" (GraphQL), background-searches ABB (worker thread, broad query ladder). Found rows are **AI-rated once, then settled** (`WANTED_LLM=false` → deterministic). Optional strict auto-download (`WANTED_AUTO_DOWNLOAD`, M4B-only).
- **Auth deployment** — behind Authentik forward-auth via Nginx Proxy Manager; `X-authentik-username` etc. arrive as request headers.
- **Security layer (v2)** — CSRF on all form POSTs (`X-CSRF-Token` header on fetches), CSP/security headers, SameSite=Lax cookies, secret key persisted under `/data`.
- **In-app settings (v2.1)** — `/settings` (gated like `/log`) edits the *feature* keys (Gemini/ABS/Hardcover/wanted) live, no restart; env vs app precedence = whichever was set most recently (see `abb/settings.py`). Deployment plumbing stays env-only, and `LOG_ADMIN_USERS` is deliberately not editable from the page it gates.

## Dev workflow (important)

- **Branch:** do work on **`dev`** and push there; open a PR to **`main`** to release. Don't commit straight to `main` unless the user explicitly says so.
- **CI / images:** every push to `main`/`dev` runs the **test job first**, then builds a multi-arch image tagged `:<branch-slug>` (e.g. `:dev`). **Only `main` also moves `:latest`.** `docker-compose.yaml` pins `:latest`, so to run dev work the user pulls the `:dev` tag.
- **Commits:** end messages with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Commit/push only when asked.
- Use the session scratchpad dir for temp files, not the repo.

## Running & testing

```bash
# Run (Docker): docker compose up -d   → http://<host>:5078
# Run (local):  cd app && python main.py    (waitress; PORT=5178 to move it)

# Before committing:
cd app && python -m pytest -q                 # the real gate (CI runs this)
node --check app/abb/static/js/app.js
python3 app/abs_match_spike.py --selftest     # matcher eval CLI, offline mode
```

## Conventions & guardrails

- **Match the surrounding code.** Modules are focused, comments explain *why*.
  JS is one IIFE with `data-action` delegation. CSS uses `tokens.css` variables —
  never hardcode colors/spacing. **Never add a CDN script tag** — icons are
  vendored (`static/images/icons.svg`); the CSP enforces `script-src 'self'`.
- **No import side effects.** Anything that starts a process/thread or touches
  disk/network belongs in a service's `init()`/`start()`, called from
  `Services.start()`. Tests rely on `create_app(start=False)` staying clean.
- **Best-effort side features never break core flow.** ABS matching, the log,
  and smart sort all swallow their own errors: a failure leaves search working,
  just without that enrichment.
- **Privacy boundaries are deliberate — preserve them:**
  - Tor shields **only** the ABB scrape (plus covers when `COVER_PROXY=true`).
    Gemini, ABS and Hardcover calls go direct.
  - Smart sort sends **only** public result metadata (`RANK_FIELDS`), never
    links/covers/hostname.
  - ABS matching **never** sends the user's library anywhere. The LLM
    canonicalizes *public* results; the ownership join runs locally.
- **Matching is precision-first:** only ever assert "you own this" when
  confident. A missing badge is never a claim you *don't* own something.
- **Compatibility:** v1 env vars, URL paths, JSON shapes, and the SQLite schema
  are a contract — don't break them casually.

## Gotchas

- **Single process only.** Waitress runs one process with threads; the rank/ABS
  caches, the wanted worker, and the managed Tor process all assume it. Don't
  front this with multi-worker gunicorn.
- The Docker image runs as **uid 1000**; a host-mounted `./data` must be
  writable by it or the log + persisted secret key degrade (with warnings).
- Templates use blueprint-qualified endpoints (`url_for('pages.search')`).
- `app/.venv/` is a local virtualenv; `memory/` is agent-local notes
  (gitignored) — neither is project code. `.DS_Store` files are noise.
- The app trusts reverse-proxy auth headers, which is only safe because it's
  reachable exclusively through the proxy. Don't add features assuming the
  header is unforgeable outside that setup.
