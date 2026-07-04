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

## Shape of the code

No build step, no framework beyond Flask, no ORM. Server-rendered Jinja +
progressive-enhancement vanilla JS.

| Path | What it is |
|---|---|
| `app/app.py` | The entire backend: config, ABB scraping, download-client registry, Tor management, smart sort (Gemini), ABS matching, download log, all routes. One big module by design. |
| `app/abs_match.py` | **Pure, stdlib-only** library matcher (no import side effects). Shared by the app and the spike. |
| `app/abs_match_spike.py` | Standalone CLI to eval/tune the matcher against a real library. Imports `abs_match`, **never** `app.py`. |
| `app/templates/` | Jinja: `base.html` (nav/shell), `search.html`, `status.html`, `log.html`, `macros/` (card, row, etc.). |
| `app/static/js/app.js` | All client JS in one IIFE. Central `document.addEventListener` delegation on `[data-action="…"]`. No modules/bundler. |
| `app/static/css/` | Design system: `tokens.css` (CSS vars — always use these), then `base`/`components`/`layout`/`search`/`status`. |
| `Dockerfile`, `docker-compose.yaml` | Deploy. Image installs Tor; app manages the tor process itself. |
| `.github/workflows/docker-publish.yml` | CI: multi-arch image per push. See workflow below. |
| `docs/` | Deep dives (architecture, library matching, development). |

## Core subsystems (one line each — details in docs)

- **Download clients** — `DOWNLOAD_BACKENDS` registry in `app.py`; one client per deploy via `DOWNLOAD_CLIENT`. Adding a client = one table entry (`add`/`list`).
- **Tor routing** — the app starts/manages its own `tor` process; ABB scrape + magnet lookup go through it, toggleable per browser (`session['route_mode']`). Everything else (Gemini, ABS, the download client) goes direct.
- **Smart sort** — optional Gemini call (`/api/rank`) that re-ranks results and groups series/editions. Only public result metadata (`RANK_FIELDS`) is sent. See [`docs/architecture.md`](docs/architecture.md).
- **ABS library matching** — flags results you already own; deterministic baseline + LLM-canonicalized local join + a live re-check poll. **Precision-first, and your library never leaves the box.** See [`docs/library-matching.md`](docs/library-matching.md).
- **Download log** — SQLite audit of who sent what; identity from reverse-proxy auth headers (Authentik). `/log` gated by `LOG_ADMIN_USERS`.
- **Hardcover wanted list** — `/wanted` dashboard syncs the user's Hardcover "Want to Read" list (GraphQL, `HARDCOVER_API_KEY`), background-searches ABB per book (worker thread, broad query ladder). Results are **AI-rated once at find time** (`_wanted_llm_verdict`; ~one call per book ever; `WANTED_LLM=false` → deterministic fallback), then persisted — found rows are settled until the user forces a re-check. Optional strict auto-download (`WANTED_AUTO_DOWNLOAD`, M4B-only). State in the log SQLite (`wanted` table).
- **Auth deployment** — runs behind Authentik forward-auth via Nginx Proxy Manager; `X-authentik-username` etc. arrive as request headers.

## Dev workflow (important)

- **Branch:** do work on **`dev`** and push there; open a PR to **`main`** to release. Don't commit straight to `main` unless the user explicitly says so.
- **CI / images:** every push to `main`/`dev` builds a multi-arch image tagged `:<branch-slug>` (e.g. `:dev`). **Only `main` also moves `:latest`.** `docker-compose.yaml` pins `:latest`, so to run dev work the user pulls the `:dev` tag.
- **Commits:** end messages with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Commit/push only when asked.
- Use the session scratchpad dir for temp files, not the repo.

## Running & testing

```bash
# Run (Docker): docker compose up -d   → http://<host>:5078
# Run (local):  cd app && python app.py   (needs a running/reachable tor for ABB)

# Fast checks before committing (there is no test framework):
python3 -m py_compile app/app.py app/abs_match.py app/abs_match_spike.py
node --check app/static/js/app.js
python3 app/abs_match_spike.py --selftest    # 9 matcher regression cases, offline
```

## Conventions & guardrails

- **Match the surrounding code.** `app.py` is intentionally one module with inline helpers and heavy explanatory comments; keep that style. JS is one IIFE with `data-action` delegation. CSS uses `tokens.css` variables — never hardcode colors/spacing.
- **Best-effort side features never break core flow.** ABS matching, the log, and smart sort all swallow their own errors: a failure leaves the search working, just without that enrichment.
- **Privacy boundaries are deliberate — preserve them:**
  - Tor shields **only** the ABB scrape. Gemini and ABS calls go direct.
  - Smart sort sends **only** public result metadata (`RANK_FIELDS`), never links/covers/hostname.
  - ABS matching **never** sends the user's library anywhere. The LLM canonicalizes *public* results; the ownership join runs locally. See the matching doc.
- **Matching is precision-first:** only ever assert "you own this" when confident. A missing badge is never a claim you *don't* own something.

## Gotchas

- **`app.py` has import-time side effects** — `init_download_log()` and `init_outbound()` (which starts Tor) run at module load. So **never `import app` from a standalone script** (it would launch a second Tor). Scripts import `abs_match` (pure) only. This is why `abs_match_spike.py` reimplements a minimal ABB scrape instead of reusing the app's.
- `app/.venv/` is a local virtualenv; `memory/` is agent-local notes (gitignored) — neither is project code.
- `.DS_Store` files exist; ignore them.
- The app trusts reverse-proxy auth headers, which is only safe because it's reachable exclusively through the proxy. Don't add features assuming the header is unforgeable outside that setup.
