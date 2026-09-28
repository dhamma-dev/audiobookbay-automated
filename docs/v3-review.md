# v3 — full review (code, features, UX)

A top-to-bottom review of v2.1, written September 2026 against `8828070`. Like
[`v2-review.md`](v2-review.md) it records findings and decisions; unlike it,
most findings here are a backlog, so each carries a **status**.

The one-line verdict: **the v2 foundation is sound, but two findings broke the
app's own core promises** — "only claim ownership when sure" (sequels were read
as the book 1 you own) and "Tor shields the ABB scrape" (every Tor failure fell
back to Direct). Both are fixed; the rest is open, ranked below.

**How this was checked:** every module, template, `app.js` and the CSS read in
full; the suite run (101 → 137 tests); each suspected bug reproduced with a
script against the real modules before being written down; and the real app
driven in a browser with every integration stubbed (no ABB, Gemini, Hardcover,
ABS or client traffic). Numbers below are measured, not estimated.

---

## What v2 got right (keep)

- The factory/services shape, zero import side effects, and a fast suite
  (`create_app(start=False)`) — refactors are cheap and safe.
- Privacy boundaries written into the code, not just the docs (`RANK_FIELDS`,
  the local ownership join, Tor for ABB only).
- UX craft: skeleton loading, series shelves with gaps, settled wanted rows,
  held picks that say *why*, provenance badges on Settings.

## Fixed in this change

| # | Finding | Consequence | Fix |
|---|---|---|---|
| M1 | Token-set similarity scores a title whose words are a subset of another's at 1.0, and nothing penalised the extra words | With only book 1s owned, all 9 sequels tried (*Dune Messiah*, *Foundation and Empire*, *He Who Fights with Monsters 5*, …) badged **In your library**; *Hide owned* hid exactly the missing books | Two demotion-only guards in `score_pair`: `volume_conflict` (declared/written numbers disagree) and `unexplained_words` (words neither title, series name, subtitle, author nor filler accounts for — an after-the-colon subtitle counts, unless the colon follows the series name). ABS `subtitle` + item `id` now indexed. All prior regression cases still pass, and so does the deliberate "subtitle-ful Hardcover title vs plain listing" match |
| M2 | Same matcher decides wanted-row ownership (`owns`, the sweep, Quick add) | A wanted sequel was marked owned **before it was ever searched** and filed on the done shelf, with no way back; Quick add answered "already in your library" | Guards above, plus **Search anyway** on done rows (`reopen`; rejected ABS items remembered in `owned_ignore`) and a once-per-worker `_verify_owned` that flags rows the current matcher no longer confirms (flag only — never an automatic re-download) |
| M3 | Smart sort's fallback join ignored the canonical number | Canonical *Dune* #1 resolved to an owned *Dune Messiah* | The fallback carries `series`/`seq` into the volume guard |
| M4 | `_series_match` compared numbers by stripping zeros as text | Book 10 == book 1, 20 == 2 (series bonus on the wrong copy) | Numeric comparison (`_norm_num`) |
| T1 | Tor *unavailable* forced every browser **and the worker** to Direct | A failed bootstrap exposed the server IP to ABB with no warning beyond a navbar label | Routing fails closed: `route_mode()` is intent only; `session_for('tor')` raises `TorUnavailable`; search/send/batch/covers refuse; the worker pauses and the dashboard says so |
| T2 | A bootstrap slower than `TOR_BOOTSTRAP_TIMEOUT` was ignored forever | Direct until a container restart | The waiter keeps listening; a late 100% flips to ready |
| T3 | A Tor process that died stayed "ready" | Every Tor request failed; renewal couldn't help | Exit flips to unavailable; relaunch with capped backoff (10 s → 5 min) |
| T4 | `WANTED_ROUTE=tor` meant "or Direct" when Tor was down | Background scrapes from the real IP | Same fail-closed path |
| T5 | While Tor was *starting*, only search was gated | **Send** and `/covers` fetched ABB directly, logged as "tor" | All ABB requests go through `session_for` |
| T6 | Auto-send fetched the magnet on the server default route | `USE_TOR=false` + `WANTED_ROUTE=tor` → detail page direct; the reverse broke auto-send on blocked exits; log recorded the wrong route | The worker's session is passed through; the log records the route used |
| T7 | Quick add / re-check with Tor down counted as a failed search | Row stamped into the 30-min retry backoff; "wasn't reachable" message | `tor-unavailable` outcome: row left due, message says it waits for Tor |
| — | Small: navbar said "Tor" after *Search directly instead*; the spike CLI scraped Direct on its own; a failed bootstrap leaked its temp dir; a failed relaunch killed relaunching | | Fixed alongside |

**Behaviour changes to know when deploying:**
- `USE_TOR=true` with no Tor now **pauses** ABB traffic instead of going
  Direct — local runs without Tor need a click on *Search directly instead*
  (remembered per browser) or `USE_TOR=false`.
- The matcher is stricter. Listings carrying words your library's metadata
  doesn't explain can lose their badge (Tier 2 — smart sort's cleaned
  identities — recovers most). Run `abs_match_spike.py` on a few series you
  own before promoting to `main`.
- The first worker sweep after deploy flags any wanted rows the old matcher
  wrongly filed as owned: check the *In your library* shelf for **Unconfirmed**.

## Open — correctness & reliability

| # | Finding | Evidence / consequence | Recommended fix |
|---|---|---|---|
| R1 | Tier-1 matching compares every result to every library item and re-normalizes the library side each time | 50 results: **3.5 s CPU/search at 1,500 books, 7 s at 3,000**; same cost in the 90 s ownership poll and the 2-min sweep | Normalize the index once at load; pre-filter on a shared token (cut pairs ~88% even on synthetic data) |
| R2 | ABS back-off only works once a snapshot exists; fetch holds the lock searches wait on; an empty home-page load triggers a fetch | ABS down/misconfigured → every search retries (5 calls → 5 fetches, 20 s timeout each) | Back off on failure regardless; skip when `books` is empty; serve stale while refreshing |
| R3 | Gemini calls have no timeout (`google-genai` defaults to `None`) | One hung call pins a request thread or **stalls the whole wanted worker** (no syncs, searches, sweeps) | **Fixed:** `GEMINI_TIMEOUT` (default 60s) via `HttpOptions`; a timeout surfaces as "Gemini didn't answer within 60s" and the wanted verdict falls back to the deterministic pick. Verified end to end against a silent socket |
| R4 | Any non-quota error permanently disables the thinking=0 path | A transient 503 makes every later rank ~4–5× slower until restart — and with R3's timeout, *every* timeout would have too, so the two were fixed together | **Fixed:** the retry-without-thinking fires only on a 400 that names the thinking config (`_rejects_thinking`); timeouts, 429s and 5xx fail once and keep the fast path |
| R5 | qBittorrent's add result (`Fails.` / failure count) is ignored | Rejected magnets report "Download added" | **Fixed:** `qbt_add_failed` reads both response shapes; a refusal or a duplicate (409) is an error with a plain message |
| R6 | Compose passes unset keys as empty strings; `Config.from_env` treats "" as a value | `DL_CATEGORY` default lost (qBittorrent/Deluge torrents uncategorized, Downloads lists every uncategorized torrent); `ABS_LOW_KBPS=""` crashes boot | **Fixed:** blank = unset in `from_env` (except `LOG_DB_PATH`, where empty is the documented off switch); a test replays the shipped compose file with nothing set |
| R7 | Transmission list ignores `DL_SCHEME` and filters nothing | HTTPS Transmission can add but not list; Downloads shows the whole client | Pass `protocol`; label on add, filter on list |
| R8 | Magnets carry no `dn=`; fallback trackers are long dead | Clients show a 40-char hash until metadata arrives | **`dn=` fixed:** every magnet carries the listing title. *Open:* refresh the fallback tracker list (only used when a detail page lists none) |
| R9 | "One Gemini call per book, ever" holds for *found* rows only | Unmatched rows are re-rated daily, on every restart, and on every *Sync now* | Store a fingerprint of the judged listings; skip the call when nothing new appeared |
| R10 | A failed auto-send (client down, magnet fetch failed) never retries | Quick add's "dip out, it shows up" silently stops at *Found* | Retry on a backoff, a few times |
| R11 | Quick add runs search → verdict → send inside the form POST | Can take a minute over Tor | Enqueue and redirect; the row shows "searching…" |
| R12 | A settings save mid-tick leaves two workers with separate in-flight sets | Possible double search/send (now narrowed: a retired worker stops between rows) | Share the in-flight set across service rebuilds |
| R13 | The M4B auto gate reads the `best_meta` text, not `is_m4b` | Title/keyword-only M4Bs held as "isn't M4B" while the card shows the ribbon | Gate on the stored candidate's `is_m4b` |
| R14 | Sync deletes every row missing from the response | A short/empty response would drop settled rows (then re-search and possibly re-download them) | Refuse to delete more than a fraction in one sync |
| R15 | `/covers`: `abort(404)` inside a broad `except` → 502; the cover cache is a module global | Cosmetic | Narrow the `except`; move the cache onto a service |

## Open — security

| # | Finding | Consequence | Recommended fix |
|---|---|---|---|
| S1 | The shipped compose publishes `5078` on all interfaces, while identity trust requires proxy-only reachability (the README's trust note says so) | Anyone on the LAN can forge `X-authentik-username`. Since v2.1 this matters more: Settings is open to everyone when `LOG_ADMIN_USERS` is unset, and changing `ABS_URL` keeps the stored `ABS_TOKEN` — the token can be re-pointed at any host | No published port (or `127.0.0.1:`) by default; optional `TRUSTED_PROXIES` check before honouring identity headers; clear/require the token when its URL changes |
| S2 | The v2 CSP (`script-src 'self'`) blocks the inline `onerror` cover fallbacks in `book_card.html` | Dead covers render as broken images / alt text (console: "Executing inline event handler violates…") | **Fixed:** `static/js/covers.js`, a capture-phase `error` listener loaded in `<head>` (app.js is deferred, so a fast failure would beat it); a test fails on any inline handler in rendered pages |

## Open — UX

| # | Finding | Recommended fix |
|---|---|---|
| U1 | **Phones:** at 375 px the nav needs ~850 px — only *Search* and *Wanted* fit, the rest (and the Tor/Direct control) are off-screen and the page scrolls sideways; the Wanted table wraps titles one word per line and pushes Send/re-check/skip off-screen | Icon-only nav under 640 px (icons exist); stacked wanted rows |
| U2 | The AJAX search never updates the URL — reload/back lose results, searches can't be shared though `GET /?q=` works | **Fixed:** each search pushes `/?q=…`; Back/Forward restore from an in-memory cache (no re-scrape), a miss reloads the URL |
| U3 | Failed form actions render a raw JSON page (e.g. *Sync now* when the Hardcover token expires on Jan 1) | Redirect with a banner |
| U4 | Downloads shows raw qBittorrent states ("MetaDL", "StalledDL") and polls every 10 s in hidden tabs | Friendly state names; skip when `document.hidden` |
| U5 | Small: log times in UTC; after *Back*, `data-busy` buttons keep spinning (bfcache); no confirm/undo on remove; a Tier-1 badge isn't cleared when Tier 2 contradicts it (rare now) | — |

## Feature ideas that fit the philosophy

- **"Ready" notifications** (ntfy/Apprise/webhook) when a wanted row flips to
  owned — completes the fire-and-forget loop.
- **Integration health on Settings**: ABS items + index age, Hardcover last
  sync + token-expiry countdown (the Jan 1 cliff), Tor state/last renewal,
  Gemini last call, client reachability — each with a *Test* button.
- **Series gap radar** beside Upgrade Radar: owned series with holes in the
  numbering, deep-linked to a search. Local arithmetic; nothing leaves the box.

## Doc drift (open)

- Startup report and README still say auto-download is "M4B only"
  (`config.py` `report()`) — it's configurable now.
- README says an empty `LOG_DB_PATH` disables logging; compose's
  `${LOG_DB_PATH:-…}` substitutes the default for an empty value.
- ~~[`v2-review.md`](v2-review.md) says the image carries its git SHA, surfaced
  at boot — nothing does.~~ **Fixed:** CI passes commit/branch/build time as
  build args (`APP_*` env, `abb/version.py`); the footer, `/healthz` and the
  boot log show them.
- Static files are served `no-cache` with ETags, so the old "hard refresh after
  deploy" advice (agent notes) no longer applies.
