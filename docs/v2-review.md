# v2 — total application review & rewrite rationale

This document is the review that drove the v2 rewrite: what v1 got right, what it
got wrong, and every decision the rewrite makes. Written July 2026 against the
last v1 commit (`b68b143`).

The one-line verdict: **the product design was right, the program shape was
wrong.** v2 keeps every feature, behavior, endpoint, and env var, and rebuilds
the codebase underneath them.

---

## What v1 got right (deliberately preserved)

- **Product philosophy.** Precision-first library matching (only ever assert a
  positive), best-effort side features that can never break search, explicit
  privacy boundaries (Tor shields only the ABB scrape; the library never leaves
  the box; only `RANK_FIELDS` go to Gemini). These are load-bearing decisions
  and v2 treats them as invariants.
- **The registry pattern** for download clients, and env-driven feature flags
  where an unset key simply hides the feature.
- **The UI.** The design system (tokens → components), skeleton loading, the
  series shelf, the wanted dashboard. v2 carries the frontend over nearly
  unchanged.
- **Operational choices**: app-managed Tor with per-browser routing, the settled
  wanted rows (one LLM call per book ever), the rank cache, request timeouts on
  every outbound call.

## Architecture findings

| # | Finding | Consequence | v2 decision |
|---|---|---|---|
| A1 | `app.py` has **import-time side effects** (`init_outbound()` starts a Tor process; `init_download_log()` touches disk) | Nothing can import the app — no tests, no scripts; the matcher spike had to reimplement scraping | **App factory.** `abb.create_app()` builds everything; importing any module does nothing |
| A2 | One 2,576-line module holding nine subsystems | Every change navigates the whole file; subsystem boundaries exist only as comment banners | **A package** with one module per subsystem, same boundaries the comments already drew |
| A3 | ~15 mutable module globals (`_tor_available`, `_rank_cache`, `_wanted_fail_streak`, …) guarded by four separate locks | Thread-safety is hand-managed and unverifiable; state can't be constructed twice (tests) | **Service classes** (`TorManager`, `AbsLibrary`, `WantedService`, …) owning their state and locks, wired together in the factory |
| A4 | **No test framework.** Only the spike's 9 offline matcher cases | Refactors are compile-checked, behavior-checked by hand | **pytest suite** (matcher, scraper fixtures, query ladder, ownership join, config, route smoke tests) run in CI before any image builds |
| A5 | Flask **dev server in production** (`app.run`) | Werkzeug's dev server is explicitly not for production (no hardening, weaker connection handling) | **Waitress** — production WSGI, pure-Python (multi-arch friendly), threaded single-process, which the app's in-memory caches and worker thread require anyway. Single-process is now a *documented* constraint |
| A6 | **Unpinned dependencies**, `python:3.10-slim` | Non-reproducible builds; any dep's breaking release breaks the next CI build silently | Pinned `requirements.txt`, `python:3.12-slim` |
| A7 | `print()` everywhere | No levels, no timestamps, needs `PYTHONUNBUFFERED` | `logging` with per-subsystem loggers (`abb.tor`, `abb.wanted`, …), `LOG_LEVEL` env |
| A8 | Tor temp data dir never removed | litter in `/tmp` across restarts | cleaned on shutdown |

## Security findings

| # | Finding | Severity | v2 decision |
|---|---|---|---|
| S1 | **Secret key is random per boot** when `FLASK_SECRET_KEY` is unset — and it's read *before* `load_dotenv()`, so setting it in `.env` was silently ignored (real bug) | Med — every restart logs users out of Put.io, forgets route choices; multi-process would break outright | Key from env, else **generated once and persisted** under the data dir (`0600`) |
| S2 | **No CSRF protection.** `/wanted/sync` and `/wanted/research/<id>` are plain form POSTs; JSON endpoints rely only on content-type; cookie SameSite unset (browser-default) | Med (low exposure behind Authentik, but defense-in-depth is the point) | Session CSRF token: hidden field on forms, `X-CSRF-Token` header on fetches, verified on **every** POST; `SameSite=Lax` + `HttpOnly` set explicitly; `SESSION_COOKIE_SECURE` opt-in (`COOKIE_SECURE=true`) |
| S3 | `GET /putio/logout` mutates state (classic CSRF-by-img); OAuth flow has **no `state` parameter** | Low/Med | Logout is a POST (with token); OAuth flow carries a random `state` checked in the callback |
| S4 | **`lucide@latest` from unpkg** — a third-party CDN script, floating tag: any future release executes in every user's browser; also fails closed (no icons) on LAN-only deploys | Med (supply chain) | **Icons vendored** as a local SVG sprite + ~40-line renderer; unpkg dependency removed. Google Fonts kept (asset, not code) and documented |
| S5 | Cover images are **hotlinked by the browser** — in Tor mode the server hides, but every user's browser IP still touches the image host | Low (documented gap in the threat model) | Optional **cover proxy** (`COVER_PROXY=true`) streaming covers through the server's route session, ABB-host-only allowlist (no open proxy). Default off: Tor-fetching ~50 covers per search is a real latency cost, so it's an informed opt-in |
| S6 | No security headers | Low | `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and a CSP (`script-src 'self'`; fonts allowed from Google) |
| S7 | Container runs as **root**, no `HEALTHCHECK` | Low | Non-root `app` user; `/healthz` + Docker `HEALTHCHECK` |
| S8 | Startup config dump prints values verbatim | Info | Config report masks secrets (`DL_PASSWORD`, tokens → `set`/`not set`) |
| S9 | *(kept, was already right)* `/send` SSRF guard (`_is_abb_link`), server-side re-sanitizing of rank payloads, `/log` self-scoping for non-admins, Jinja autoescape + JS `escapeHtml` | — | preserved verbatim |

The header-trust model (Authentik forward-auth headers, only safe behind the
proxy) is unchanged and still documented as such.

## Capability & UX findings

- **UX is strong**; v2 changes almost nothing visible. Kept: every page, flow,
  and interaction. Added: `/` focuses the search box; icons render offline
  (S4); `<noscript>` notice (the app is JS-dependent); reduced-motion already
  respected by the CSS.
- **Ops gaps**: no health endpoint (added `/healthz`), no build identity
  (image now carries the git SHA, surfaced at boot), no `.env.example`
  (added — README stays the narrative reference, the example file is the
  copy-paste one).
- **SQLite**: now opened WAL + `busy_timeout`, with indexes on
  `downloads(user)` / `downloads(ts)`; same schema, same self-migration.

## v2 shape

```
app/
  main.py                  entrypoint: create_app() + waitress
  requirements.txt         pinned
  abb/                     the application package
    __init__.py            create_app() — config, services, blueprints, security
    config.py              typed Config.from_env(), validation, masked report
    tor.py                 TorManager (launch/bootstrap/renew/status)
    outbound.py            Outbound: direct+tor sessions, per-request route mode
    scraper.py             ABB search page parse/fetch, magnet extraction
    storage.py             SQLite: download log + wanted rows (WAL)
    matching.py            the pure matcher (v1 abs_match.py, unchanged logic)
    library.py             AbsLibrary: index cache, quality flags, ownership joins
    smart_sort.py          RankService: Gemini schemas, cache, wanted verdict
    wanted.py              WantedService: Hardcover sync, worker, pipeline
    clients.py             download-client registry (qbit/transmission/deluge/put.io)
    identity.py            proxy-header identity + log-admin check
    security.py            CSRF, headers, persisted secret key
    web/                   blueprints: pages.py, actions.py, api.py, putio.py
    templates/  static/    carried over from v1 (+ vendored icons, CSRF wiring)
  tools/abs_match_spike.py the eval CLI (now imports abb.matching directly)
  tests/                   pytest
```

**Compatibility contract:** every v1 env var, URL path, JSON response shape,
and the SQLite schema work unchanged. A v1 `.env` + `data/` volume drops into
v2 as-is. The only removed things are the unpkg script tag and the
`GET /putio/logout` method (now POST).

## Explicitly considered and rejected

- **FastAPI / async rewrite** — the app is I/O-light (a family-scale instance),
  the ecosystem deps (`qbittorrent-api`, tor management, `requests[socks]`)
  are sync, and async would have rewritten risk into every subsystem for
  latency nobody is waiting on. Threads are the right size.
- **An ORM / Postgres** — two tables and an audit log; SQLite WAL is correct.
- **A JS framework/bundler** — the IIFE + `data-action` delegation is small,
  fast, and dependency-free; v2 keeps it.
- **Multi-worker serving** — in-memory caches, the wanted worker, and the
  managed Tor process all assume one process. Documented instead of "fixed".
- **Self-hosting fonts** — fonts are static assets from a stable origin, not
  executable code; the supply-chain risk that forced vendoring the icon
  *script* doesn't apply. Revisit if the privacy stance tightens.
