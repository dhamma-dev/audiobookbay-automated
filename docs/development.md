# Development

Workflow, testing, and deploy details. The essentials are in
[`../CLAUDE.md`](../CLAUDE.md); this is the expanded version.

## Branching & release

- **Work on `dev`.** Push feature work there. Open a PR from `dev` → `main` to
  release. Only touch `main` directly if the user explicitly asks.
- History is otherwise messy (many stale `feature/*` branches from earlier
  work); `dev` and `main` are the only ones that matter.

## CI & images (`.github/workflows/docker-publish.yml`)

- Triggers on push/PR to `main` and `dev` (and manual dispatch).
- **Two jobs: `test` gates `build`.** The test job runs pytest (against the
  pinned `requirements-dev.txt`) and `node --check` on the client JS; no image
  is built, let alone pushed, unless it's green.
- The build job produces a **multi-arch** (amd64+arm64) image and pushes to
  GHCR: `ghcr.io/dhamma-dev/audiobookbay-automated`.
- Tagging: every build gets `:<branch-slug>` (e.g. `:dev`). **Only `main` also
  moves `:latest`.** PRs build for validation but don't push.
- Consequence: `docker-compose.yaml` pins `:latest` (= `main`). To run/test
  `dev` work, pull the `:dev` tag.

## Running

- **Docker (normal):** `docker compose up -d` → `http://<host>:5078`. The image
  bundles `tor`; the app launches and manages the tor process itself. The
  container runs as **uid 1000** and reports health via `/healthz`.
- **Local:** `cd app && python main.py` (waitress on port 5078; `PORT=5178` to
  move it). Needs a reachable tor for ABB search unless the user switches to
  Direct in the UI. A local `app/.venv/` exists.
- **Run a script inside the live container** (e.g. the matcher spike), which
  reuses the app's Tor and `.env`:
  `docker compose exec audiobookbay-automated python abs_match_spike.py --selftest`

## Testing

```bash
cd app && python -m pytest -q          # the suite CI runs (needs requirements-dev.txt)
node --check app/abb/static/js/app.js  # and icons.js if you touched it
python3 app/abs_match_spike.py --selftest   # matcher eval CLI, offline mode
```

The suite covers the pure logic (matcher regressions, scraper fixtures, the
wanted query ladder and scheduling, the ownership join, config validation) and
route smoke tests through Flask's test client with `create_app(start=False)`
(no Tor, no DB, no worker). Add a test with any behavioral change — CI blocks
the image otherwise.

For ad-hoc exploration of matcher behavior against a real library, the spike
CLI (`abs_match_spike.py`) does live runs; it imports `abb.matching` only.

When a change is behavioral against external services (ABB markup, download
clients, Hardcover), say so and confirm it live rather than claiming it works
from tests alone.

## Configuration

All config is env vars, parsed once by `Config.from_env()` in
`app/abb/config.py` — that file is the authoritative list, the README the
narrative version, and [`.env.example`](../.env.example) the copy-paste one.
Broad groups: download client (`DOWNLOAD_CLIENT` + its `DL_*`/`PUTIO_*`), Tor
(`USE_TOR`, `TOR_*`), smart sort (`GEMINI_API_KEY`, `RANK_MODEL`), download
log (`LOG_DB_PATH`, `LOG_ADMIN_USERS`), ABS matching (`ABS_*`), wanted list
(`HARDCOVER_API_KEY`, `WANTED_*`), server (`FLASK_SECRET_KEY`, `LOG_LEVEL`,
`COOKIE_SECURE`, `COVER_PROXY`, `PORT`).

Persistent state lives on the `./data` volume: the SQLite log/wanted DB and
the auto-generated `secret_key` file. Nothing else is persisted.

## Conventions

- **Match the file you're in.** Modules are focused with generous *why*
  comments. Registries over branching. Keep it.
- JS: one IIFE, `data-action` delegation, no dependencies added lightly
  (runtime deps: Flask, waitress, requests[socks], bs4, the three
  torrent-client libs, python-dotenv, google-genai — all pinned).
  `abb/matching.py` is deliberately stdlib-only. **No CDN scripts** — the CSP
  enforces `script-src 'self'`; icons are a vendored sprite.
- CSS: `tokens.css` variables only; reuse existing card/badge/button/series
  classes.
- Side features are best-effort and must never break search.
- No import side effects — new services get constructed in `Services` and
  started in `Services.start()`.
- Commit message trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
  PR bodies end with the "Generated with Claude Code" line.

## Gotchas (repeat of the important ones)

- **One process.** Waitress + threads. The caches, the wanted worker, and the
  managed Tor process assume it — never front with multi-worker gunicorn.
- Host-mounted `./data` must be writable by uid 1000 (`chown -R 1000:1000
  data`) or the log and persisted secret key degrade (loud warnings, app still
  runs).
- Search results are not stored server-side; ranking/ownership endpoints
  receive what the client posts back.
- Auth-header identity (Authentik) is only trustworthy because the app is
  reachable exclusively via the proxy. Don't build on the header being
  unforgeable outside that setup.
- `memory/` (agent notes) and `app/.venv/` are gitignored and not project
  code; `folder/` is a stray empty dir; `.DS_Store` files are noise.
