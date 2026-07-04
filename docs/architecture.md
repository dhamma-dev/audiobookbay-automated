# Architecture

How the pieces fit together and how a request flows. Read
[`../CLAUDE.md`](../CLAUDE.md) first for the file map and conventions.

## Big picture

```
Browser ──HTTP──> Flask (app.py) ──scrape (via Tor)──> AudioBook Bay
                     │  ├── magnet build (via Tor) ──> ABB detail page
                     │  ├── /send ─────────────────> download client (qbit/…/Put.io)
                     │  ├── /api/rank ─────────────> Google Gemini (public metadata only)
                     │  ├── ABS index / ownership ─> Audiobookshelf API (direct, local join)
                     │  └── /log ──────────────────> SQLite (download audit)
                     └── serves Jinja HTML + one JS file + CSS design system
```

There is no database except the SQLite download log. Search results are **not**
persisted server-side — the client holds them and posts back what it needs
(this is why the ranking/ownership endpoints receive result metadata rather than
looking it up).

## Backend layout (`app/app.py`)

The module reads top-to-bottom as: imports → config (env vars, one block per
feature, each with a comment header) → helper/feature functions → Flask routes →
`init_*()` calls at the bottom that run **at import time**.

Key regions (search by name; line numbers drift):

- **Config blocks** — `USE_TOR`/`TOR_*`, download-client selection
  (`DOWNLOAD_CLIENT`, `CLIENT_REQUIRED_ENV`, `_validate_client_config`), Put.io
  OAuth, `GEMINI_API_KEY`/`RANK_MODEL`, download log (`LOG_*`), ABS matching
  (`ABS_*`). Each is guarded so a misconfigured deploy fails loudly or disables
  just that feature.
- **ABB scraping** — `search_audiobookbay(query)` (paginated `.post` scrape →
  list of book dicts) and `extract_magnet_link(details_url)` (reads Info Hash +
  trackers off the detail page, builds a magnet). Both use `scrape_session()`,
  which is Tor-proxied or direct per the user's route mode.
- **Download clients** — `DOWNLOAD_BACKENDS = {name: {add, list}}`. `/send`
  dispatches through it; `/status` + `/api/status` list through it. Add a client
  by adding one entry.
- **Smart sort** — `rank_payload`, `RANK_SYSTEM_INSTRUCTION`,
  `RANK_RESPONSE_SCHEMA`, `rank_results`, `/api/rank`. See below.
- **ABS matching** — `get_abs_index`, `annotate_library_matches`,
  `resolve_ownership`, `/api/ownership`. See [`library-matching.md`](library-matching.md).
- **Download log** — `current_user_label` (identity from proxy headers),
  `record_download`, `fetch_download_log`, `/log`.
- **Routes** — `/` (search), `/send`, `/status`, `/api/status`, `/api/rank`,
  `/api/ownership`, `/log`, `/settings/route`, `/tor/renew`, Put.io OAuth.
- **Bottom of file** — `init_download_log()` and `init_outbound()` (starts/join
  Tor, builds sessions) execute on import so WSGI servers get them too.

## Request lifecycles

### Search (`POST /`)
1. `search_audiobookbay(query)` scrapes ABB (through Tor unless the user chose
   Direct). Page 1 is fetched first; pages 2..N are fetched **concurrently**
   (results keep page order, first empty page still ends the run), and every
   fetch is bounded by `REQUEST_TIMEOUT` (default 45s) so one stalled Tor
   stream can't hang a search.
2. Results are sorted (M4B and preferred-language float to the top), each gets a
   stable integer `id`.
3. `annotate_library_matches(books)` adds `book['library_match']` for confidently
   owned results (deterministic pass).
4. `search.html` renders a card per result via the `book_card` macro, plus a
   slim JSON payload (`#search-results-data`) the client reuses for smart sort
   and ownership polling.

### Send (`POST /send`)
JSON `{link, title}` → `extract_magnet_link` → `DOWNLOAD_BACKENDS[client]['add']`
→ `record_download(...)`. Errors are returned as JSON messages; the log records
success and failure.

### Smart sort (`POST /api/rank`, optional)
Client posts `{query, results}` (the slim payload). Server re-sanitizes to
`id + RANK_FIELDS`, calls `rank_results`, returns a JSON verdict the client
applies **without re-rendering** — it reorders/wraps the existing cards.

`rank_results` (temperature 0, structured `RANK_RESPONSE_SCHEMA`; reasoning
capped by `RANK_THINKING_BUDGET` — default 0, with a retry-without-it fallback;
completed rankings are cached server-side for `RANK_CACHE_TTL` so re-searches
and second tabs are instant; logs `[SMART SORT] … in Xs`) asks Gemini for:
- `ordering` + `buckets` (strong/possible/unlikely) — relevance sort + filtering.
- `ambiguous` + `interpretations` — clickable "did you mean" chips.
- `series` — ordered entries (seq/title/best_id/alt_ids), `collections`
  (omnibus with `covers`), and `total`; renders as a "shelf" with per-book
  selection and a batch send.
- `editions` — group multiple uploads of one standalone work, best + alts.
- `canonical` — **only when ABS matching is on**; per-result clean identity used
  for the local ownership join (see the matching doc).

Privacy: only `RANK_FIELDS` (`title, format, bitrate, language, size, keywords`)
are ever sent. The feature is invisible unless `GEMINI_API_KEY` is set.

**Speculative prefetch.** Because the call takes a few seconds, a per-browser
setting ("Prepare automatically", persisted via `/settings/prefetch` →
`session['smart_prefetch']`, default `SMART_PREFETCH_DEFAULT`) can run it in the
background as soon as results render. Client side (`app.js`): `fetchRanking`
de-dupes the call into one in-flight promise per (query, result set), so a
background prefetch and a click share it — clicking while "Preparing…" simply
attaches and applies when it lands. `setSmartBtnState` drives the button
(idle → preparing → ready/applying → applied); `prefetchSmartSort`/`initSmartSort`
kick it after each render when enabled. Results already have a per-query client
cache (`smartSortCache`), so re-sorting is instant.

## Frontend model (`app/static/js/app.js`)

One IIFE. Global event delegation: a single `click`/`change`/`submit` listener
matches `e.target.closest('[data-action="…"]')` and dispatches. To add an
interaction: add `data-action="foo"` in the template and a branch in the
delegator.

- **Search** is AJAX (`handleSearch`) — it fetches `/` and swaps the
  `#search-results` innerHTML, so server-rendered enrichments (badges, the data
  payload) come along and the delegated handlers keep working.
- **Smart sort** (`handleSmartSort` → `applyRanking`) calls `renderAmbiguity`,
  `renderSeries`, `renderEditions`, `renderSmartSort`, then `applyOwnership`. It
  moves existing cards into group containers rather than rebuilding them, so
  server-rendered ownership badges survive.
- **Series grouping** (`renderSeries`, `buildSeriesEntry`, `updateSeriesCount`,
  `syncCollections`) builds the shelf with checkboxes; ownership integrates by
  un-ticking owned entries (see the matching doc).
- **Ownership** (`markOwnedCard`, `refreshOwnershipCounts`, `applyOwnership`,
  `pollOwnership`) — shared by the initial apply and the periodic re-check.

Design-system CSS: use `tokens.css` variables. Cards, badges, buttons, and the
series shelf all have established classes — reuse them.
