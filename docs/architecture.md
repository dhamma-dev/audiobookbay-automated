# Architecture

How the pieces fit together and how a request flows. Read
[`../CLAUDE.md`](../CLAUDE.md) first for the file map and conventions, and
[`v2-review.md`](v2-review.md) for why v2 is shaped this way.

## Big picture

```
Browser ──HTTP──> Flask (abb package) ──scrape (via Tor)──> AudioBook Bay
                     │  ├── magnet build (via Tor) ──> ABB detail page
                     │  ├── /send ─────────────────> download client (qbit/…/Put.io)
                     │  ├── /api/rank ─────────────> Google Gemini (public metadata only)
                     │  ├── ABS index / ownership ─> Audiobookshelf API (direct, local join)
                     │  ├── Hardcover sync ────────> api.hardcover.app (direct)
                     │  └── /log ──────────────────> SQLite (download audit + wanted rows)
                     └── serves Jinja HTML + one JS file + CSS design system
                         (icons vendored — no CDN; CSP: script-src 'self')
```

There is no database except the SQLite file (download log + wanted rows).
Search results are **not** persisted server-side — the client holds them and
posts back what it needs (this is why the ranking/ownership endpoints receive
result metadata rather than looking it up).

## Backend layout (`app/abb/`)

Everything is wired by the **app factory** (`factory.py`): `create_app()`
builds a `Config`, constructs the `Services` container, registers blueprints,
and then — and only then — runs the side effects (`Services.start()`: DB init,
Tor launch, wanted worker). Importing any module does nothing, so tests and
scripts can import freely; `create_app(start=False)` gives a fully-wired app
with no side effects.

One module per subsystem; each service class owns its own state and locks:

- **`config.py`** — `Config.from_env()`: every env var (v1 names/defaults
  preserved), `validate_client()` (a bad DOWNLOAD_CLIENT fails loudly at boot
  and in-app), `report()` (startup summary, secrets masked).
- **`tor.py` / `outbound.py`** — `TorManager` launches/bootstraps tor in the
  background and renews circuits via the control port; `Outbound` holds the
  direct + Tor `requests` sessions and resolves the per-request route
  (`session['route_mode']`, else the `USE_TOR` default).
- **`scraper.py`** — `parse_search_page` (pure: HTML → book dicts, tested with
  fixtures); `Scraper.search` (page 1 first, pages 2..N concurrently, `None` =
  mirror unreachable vs `[]` = no results); `extract_magnet_link` (Info Hash +
  trackers → magnet).
- **`storage.py`** — `Store`: the `downloads` and `wanted` tables (v1 schema,
  additive self-migration), WAL + busy-timeout, in-memory wanted fallback when
  the log is disabled.
- **`matching.py`** — the pure matcher (see
  [`library-matching.md`](library-matching.md)).
- **`library.py`** — `AbsLibrary`: cached ABS index (`get_index(max_age)`),
  deterministic badges (`annotate_matches`), the local ownership join
  (`resolve_ownership`), Upgrade Radar (`quality_flag` / `flagged_items`).
- **`smart_sort.py`** — `RankService`: prompts + JSON schemas, the rank cache,
  the thinking-budget fallback, and the wanted verdict. `RANK_FIELDS` is the
  only data that ever reaches Gemini.
- **`wanted.py`** — `WantedService`: Hardcover GraphQL sync, the broad query
  ladder (`wanted_queries`), the worker loop (≤3 searches/minute-tick,
  auto circuit renewal after repeated unreachable scrapes), settled found
  rows, strict auto-download.
- **`clients.py`** — the download-client registry; `identity.py` — proxy-header
  identity; `security.py` — CSRF/headers/secret key (see below).
- **`web/`** — blueprints: `pages` (HTML pages + the optional `/covers`
  proxy), `actions` (`/send`, `/send/batch`, settings, `/tor/renew`, wanted
  actions), `api` (`/api/rank`, `/api/ownership`, `/api/status`,
  `/api/connection`, `/healthz`), `putio` (OAuth with `state`, POST logout).

## Request lifecycles

### Search (`POST /`, or shareable `GET /?q=…`)
1. `Scraper.search(query)` scrapes ABB (through Tor unless the user chose
   Direct). Page 1 is fetched first; pages 2..N are fetched **concurrently**
   (results keep page order, first empty page ends the run), every fetch
   bounded by `REQUEST_TIMEOUT` (default 45s).
2. Results are sorted (preferred-language and M4B float to the top), each gets
   a stable integer `id`.
3. `AbsLibrary.annotate_matches(books)` adds `book['library_match']` for
   confidently owned results (deterministic pass).
4. `search.html` renders a card per result, plus a slim JSON payload
   (`#search-results-data`) the client reuses for smart sort and ownership
   polling. With `COVER_PROXY=true`, ABB-hosted cover URLs are rewritten to
   `/covers?u=…` so the *browser* never touches the mirror either.

### Send (`POST /send`)
JSON `{link, title}` → ABB-host check (SSRF guard) → `extract_magnet_link` →
`ClientRegistry.add` → `Store.record_download`. Errors return JSON messages;
the log records success and failure. `/send/batch` does the same per item
under one `batch_id`.

### Smart sort (`POST /api/rank`, optional)
Client posts `{query, results}` (the slim payload). Server re-sanitizes to
`id + RANK_FIELDS`, calls `RankService.rank`, returns a JSON verdict the client
applies **without re-rendering** — it reorders/wraps the existing cards.

`rank` (temperature 0, structured schema; reasoning capped by
`RANK_THINKING_BUDGET` — default 0, with a retry-without-it fallback; completed
rankings cached for `RANK_CACHE_TTL`) asks Gemini for:
- `ordering` + `buckets` (strong/possible/unlikely) — relevance sort + filtering.
- `ambiguous` + `interpretations` — clickable "did you mean" chips.
- `series` — ordered entries (seq/title/best_id/alt_ids), `collections`
  (omnibus with `covers`), and `total`; renders as a "shelf" with per-book
  selection and a batch send.
- `editions` — group multiple uploads of one standalone work, best + alts.
- `canonical` — **only when ABS matching is on**; per-result clean identity
  used for the local ownership join (see the matching doc).

Privacy: only `RANK_FIELDS` (`title, format, bitrate, language, size,
keywords`) are ever sent. The feature is invisible unless `GEMINI_API_KEY` is
set.

**Speculative prefetch.** Because the call takes a few seconds, a per-browser
setting ("Prepare automatically", persisted via `/settings/prefetch` →
`session['smart_prefetch']`, default `SMART_PREFETCH_DEFAULT`) can run it in
the background as soon as results render. Client side (`app.js`):
`fetchRanking` de-dupes the call into one in-flight promise per (query, result
set), so a background prefetch and a click share it. `setSmartBtnState` drives
the button (idle → preparing → ready/applying → applied).

## Security model (v2)

Defense-in-depth behind the Authentik proxy (which remains the actual gate):

- **Secret key** — `FLASK_SECRET_KEY`, else generated once and persisted at
  `<data dir>/secret_key` (0600), so sessions survive restarts.
- **CSRF** — per-session token; form POSTs carry a hidden `csrf_token`, JS
  fetches send `X-CSRF-Token` (see `jsonHeaders()` in `app.js`). Tokenless
  JSON POSTs are allowed *only* with a real `application/json` content type
  (unforgeable cross-site without a preflight), which keeps `/send` scriptable.
- **Headers** — CSP (`script-src 'self'` — hence vendored icons), `nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`. Cookies are
  `HttpOnly` + `SameSite=Lax` (+ `Secure` with `COOKIE_SECURE=true`).
- **SSRF guards** — `/send` only fetches ABB-host links; `/covers` only
  proxies ABB-hosted images.
- **put.io OAuth** — `state` parameter verified in the callback; logout is a
  POST.

## In-app settings (v2.1)

`/settings` (blueprint `web/admin.py`, gated like `/log`) edits the
feature-tier keys — `config.FEATURE_SETTINGS`: Gemini, ABS, Hardcover, the
wanted toggles — without a redeploy. Overrides live in the `settings` table;
precedence is **most-recently-set-wins**, made deterministic by snapshotting
the env value at save time: if the env var changed since, env wins and the
override shows as "superseded" (`abb/settings.py`). Saving calls
`Services.reload_settings()`, which recomputes the effective config and
rebuilds the rank/library/wanted services (the retired wanted worker exits at
its next tick). Deployment plumbing (client creds, Tor, LOG_*, PORT) is
deliberately env-only, secrets are write-only in the UI, and `LOG_ADMIN_USERS`
cannot be edited from the page it gates.

## Frontend model (`app/abb/static/js/app.js`)

One IIFE. Global event delegation: a single `click`/`change`/`submit` listener
matches `e.target.closest('[data-action="…"]')` and dispatches. To add an
interaction: add `data-action="foo"` in the template and a branch in the
delegator.

- **Search** is AJAX (`handleSearch`) — it fetches `/` and swaps the
  `#search-results` innerHTML, so server-rendered enrichments (badges, the
  data payload) come along and the delegated handlers keep working.
- **Smart sort** (`handleSmartSort` → `applyRanking`) calls `renderAmbiguity`,
  `renderSeries`, `renderEditions`, `renderSmartSort`, then `applyOwnership`.
  It moves existing cards into group containers rather than rebuilding them.
- **Series grouping** builds the shelf with checkboxes; ownership integrates by
  un-ticking owned entries (see the matching doc).
- **Ownership** (`markOwnedCard`, `refreshOwnershipCounts`, `applyOwnership`,
  `pollOwnership`) — shared by the initial apply and the periodic re-check.
- **Icons** — `icons.js` renders `<i data-lucide="name">` from the vendored
  sprite and exposes `window.lucide.createIcons` for compatibility. Add new
  icons by appending a `<symbol>` to `static/images/icons.svg` (lucide-static,
  ISC).

Design-system CSS: use `tokens.css` variables. Cards, badges, buttons, and the
series shelf all have established classes — reuse them.
