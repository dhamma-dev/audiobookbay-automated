# Development

Workflow, testing, and deploy details. The essentials are in
[`../CLAUDE.md`](../CLAUDE.md); this is the expanded version.

## Branching & release

- **Work on `dev`.** Push feature work there. Open a PR from `dev` → `main` to
  release. Only touch `main` directly if the user explicitly asks.
- History is otherwise messy (many stale `feature/*` branches from earlier work);
  `dev` and `main` are the only ones that matter.

## CI & images (`.github/workflows/docker-publish.yml`)

- Triggers on push/PR to `main` and `dev` (and manual dispatch).
- Builds a **multi-arch** (amd64+arm64) image and pushes to GHCR:
  `ghcr.io/dhamma-dev/audiobookbay-automated`.
- Tagging: every build gets `:<branch-slug>` (e.g. `:dev`). **Only `main` also
  moves `:latest`.** PRs build for validation but don't push.
- Consequence: `docker-compose.yaml` pins `:latest` (= `main`). To run/test `dev`
  work, pull the `:dev` tag. "Pull the rebuilt dev image" means: wait for CI on
  the `dev` push, then `docker compose pull` (with the tag pointed at `:dev`).

## Running

- **Docker (normal):** `docker compose up -d` → `http://<host>:5078`. The image
  bundles `tor`; the app launches and manages the tor process itself.
- **Local:** `cd app && python app.py` (Flask dev server, port 5078). Needs a
  reachable tor for ABB search unless the user switches to Direct in the UI. A
  local `app/.venv/` exists.
- **Run a script inside the live container** (e.g. the matcher spike), which
  reuses the app's Tor and `.env`:
  `docker compose exec audiobookbay-automated python abs_match_spike.py --selftest`

## Testing

There is **no test framework**. Use these fast checks before committing:

```bash
python3 -m py_compile app/app.py app/abs_match.py app/abs_match_spike.py
node --check app/static/js/app.js
python3 app/abs_match_spike.py --selftest      # 9 matcher regression cases
```

For logic that's awkward to test through the app (e.g. the ownership join),
prefer a small standalone Python snippet that imports **`abs_match` only** and
exercises the pure functions with a fake index — do **not** import `app.py`
(it starts Tor at import). This is how the matcher/join have been validated.

When a change is behavioral (async, UI, external services), say so and confirm
it live rather than claiming it works from a compile alone.

## Configuration

All config is env vars, read at the top of `app.py`, documented in the README
and `docker-compose.yaml` comments. There is **no `.env.example`** — the README
is the source of truth. Broad groups: download client (`DOWNLOAD_CLIENT` + its
`DL_*`/`PUTIO_*`), Tor (`USE_TOR`, `TOR_*`), smart sort (`GEMINI_API_KEY`,
`RANK_MODEL`), download log (`LOG_DB_PATH`, `LOG_ADMIN_USERS`), ABS matching
(`ABS_URL`, `ABS_TOKEN`, `ABS_LIBRARY_ID`, `ABS_CACHE_TTL`).

Persistent state (the SQLite log) lives on the `./data` volume; nothing else is
persisted.

## Conventions

- **Match the file you're in.** `app.py` favors one module, inline helpers,
  registries over branching, and generous comments explaining *why*. Keep it.
- JS: one IIFE, `data-action` delegation, no dependencies added lightly (current
  runtime deps: Flask, requests[socks], bs4, the three torrent-client libs,
  python-dotenv, google-genai). `abs_match.py` is deliberately stdlib-only.
- CSS: `tokens.css` variables only; reuse existing card/badge/button/series
  classes.
- Side features are best-effort and must never break search.
- Commit message trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
  PR bodies end with the "Generated with Claude Code" line.

## Gotchas (repeat of the important ones)

- **Do not `import app` from a script** — module-level `init_outbound()` starts a
  tor process. Scripts import `abs_match` (pure). `abs_match_spike.py` therefore
  has its own minimal ABB scrape.
- Search results are not stored server-side; ranking/ownership endpoints receive
  what the client posts back.
- Auth-header identity (Authentik) is only trustworthy because the app is
  reachable exclusively via the proxy. Don't build on the header being
  unforgeable outside that setup.
- `memory/` (agent notes) and `app/.venv/` are gitignored and not project code;
  `folder/` is a stray empty dir; `.DS_Store` files are noise.
