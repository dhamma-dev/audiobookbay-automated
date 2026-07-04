# "In your library" — Audiobookshelf matching

Flags search results the user already owns in Audiobookshelf (ABS), so they can
fill gaps instead of re-downloading. This is the most involved subsystem; read
this before changing any `abs_match*` code or the ownership endpoints.

## Guiding philosophy (do not violate)

1. **Precision-first — only ever assert a positive.** We badge "In your library"
   only when confident. Everything else shows **nothing**. A missing badge is
   *not* a claim the user doesn't own it — so a slight title variation can never
   mislead. This is a deliberate product decision: false negatives (no badge)
   are cheap; false positives (wrong badge / re-download) are not.
2. **The user's library never leaves the box.** We may send *public* ABB result
   metadata to Gemini (that's smart sort), but never anything about what the
   user owns. The LLM canonicalizes public results; the ownership **join runs
   locally** against the ABS index.
3. **Best-effort.** Any failure (ABS down, bad response) leaves search working,
   just without badges.

## Two tiers

**Tier 1 — deterministic baseline (always on when `ABS_URL`+`ABS_TOKEN` set).**
Runs on every search, no LLM, private, fast. Produces the confident badge.

**Tier 2 — LLM-assisted (only when the user runs Smart sort).** Gemini
canonicalizes messy public titles (resolving `Waybound` → Cradle #12, author
variants); the app joins those clean identities to the library locally. This is
where recall improves and series-aware "own N of M" / partial-bundle ownership
come from. No library data is sent — the LLM is *not* told what the user owns.

Plus a **live poll** that re-checks un-owned results in place so a book that
finishes downloading flips to owned without a re-search.

## The matcher — `app/abs_match.py` (pure, stdlib only)

No import side effects; usable from the app and the spike. Key pieces:

- `normalize`, `tokens`, `token_set_ratio` — text similarity via `difflib`
  (rapidfuzz-style token-set ratio, no dependency).
- `author_similarity` — **surname-weighted** and multi-author aware. Two names
  only count as the same author if surnames agree (compatible first initial); a
  shared first name alone scores below the gate. This is what stops
  "same title, different author" false positives.
- `score_pair(abb, item)` → `(tier, score, reason)` where tier is
  `STRONG`/`MAYBE`/`NONE`. A strong title match is **gated on the author**;
  guards then *demote* (never promote):
  - **Foreign-edition guard** (`foreign_edition`) — a non-English edition
    (scraped `Language` field or `[Spanish Edition]`-style title marker) doesn't
    match the owned English copy.
  - **Bundle/range guard** (`is_multi_volume`) — a bundle/box set matched
    against a single owned volume is demoted (you own book 10, not the 1–10
    bundle).
- `best_match(abb, items)` → best `(tier, score, item, reason)`.
- `candidates(abb, items, k)` → top-k scored items (the cheap "blocker"; kept for
  a possible future LLM-verify path).
- `split_title_author(raw)` — ABB titles are usually `Title - Author`.

**Tuning knobs** are module-level constants at the top: `STRONG_TITLE`,
`MAYBE_TITLE`, `AUTHOR_MIN`, `SERIES_BONUS`. Only `STRONG` ever becomes a badge.

## App integration — `app/app.py`

- `get_abs_index(max_age=None)` — fetches + caches the ABS library in memory
  (`ABS_CACHE_TTL`, default 900s). `max_age` forces a fresher snapshot but still
  fetches ABS at most once per that window across all callers (used by the poll
  with `max_age=120`). Keeps the last good snapshot on error.
- `annotate_library_matches(books)` — Tier 1. Sets `book['library_match'] =
  {title, author}` for `STRONG` matches. Called in the search route.
- `resolve_ownership(ranking, index)` — Tier 2. Joins the LLM's `canonical`
  identities to the ABS index locally, adds `ranking['ownership'] =
  [{id, status, detail}]`. Series books join on **(fuzzy series name, seq)**;
  standalones via `best_match`; omnibus `collections` via covers ∩ owned-seqs →
  `partial "N of M"`. Shared helper `_canonical_owned`.
- **`/api/rank`** — when ABS is on, requests the `canonical` block
  (`rank_results(..., want_ownership=True)` adds `RANK_CANONICALIZE_INSTRUCTION`
  + `_rank_schema_with_canonical()`), then runs `resolve_ownership`. The non-ABS
  path is byte-identical.
- **`/api/ownership`** — the live poll endpoint. Client posts identities of the
  results it currently shows as un-owned; server does the local join against a
  throttled-fresh index and returns which are now owned.

## Ownership statuses

| status | badge | selection behavior |
|---|---|---|
| `owned` | "In your library" (green) | series entry unticked + dimmed |
| `owned_other_edition` | "In library · other edition" | same as owned |
| `partial` | "Own N of M" (amber) | stays selectable (you lack some) |
| `upgrade` | "Upgrade available · yours is …" (amber) | **stays ticked** — replacing junk is a wanted download |
| *(none)* | no badge | normal |

## Upgrade radar (owned-copy quality)

The ABS index also carries each item's `size`, `duration`, `tracks`, and a
computed `est_kbps` (= size×8/duration — ABS doesn't expose bitrate on the
listing, but this effective rate is all we need). `_quality_flag(item)` returns
a short reason ("~48 kbps", "27 files") when a copy is at/below `ABS_LOW_KBPS`
(default 63) or has ≥ 8 files; `_is_upgrade_result(book, item)` is true when the
owned copy is flagged AND the result is M4B AND its stated bitrate isn't worse.
All local arithmetic; nothing is transmitted.

Surfaces:
- **`/upgrades`** — worst-first table of flagged copies with a "Find better"
  deep link (`/?q=<title author>`; the search route accepts GET `?q=` for this,
  which also makes searches shareable).
- **Deterministic badge** — `annotate_library_matches` sets
  `library_match.upgrade` + `note`; the card renders the amber flag.
- **Smart sort** — `resolve_ownership(..., results)` reports `upgrade` instead
  of `owned` per result id (alts judged against their own format), and
  `markOwnedCard` keeps upgrade entries ticked in series shelves.
- "Hide owned" does **not** hide upgrades (they're actionable, not redundant).

## UI — `book_card.html`, `search.html`, `app.js`, CSS

- Badge renders server-side under the title (`.library-flag`, hooks:
  card `data-in-library`, inner `data-library-flag` + `.library-flag-text`) so
  the client can **upgrade** it later.
- **Series shelves are a "complete my collection" tool:** owned entries are
  **unticked by default** and dimmed (`.series-entry.is-owned`), so "Send N
  selected" counts only what's missing. "Hide owned" collapses the whole owned
  entry row (not just its card) and, because owned rows are unticked, can't
  silently send hidden books.
- Client ownership functions (`app.js`): `markOwnedCard` (badge + series entry
  treatment), `refreshOwnershipCounts` (`updateSeriesCount` +
  `updateSeriesOwnedNote` + `updateOwnedSummary`), `applyOwnership` (initial,
  from a smart-sort ranking), and `pollOwnership`.
- **Live poll:** `setInterval(pollOwnership, 90000)`. Sends only un-owned on-page
  cards; paused when the tab is hidden; reuses the smart-sort `canonical`
  identities (`ownershipIdentities`) so there's **no new LLM call**; the identity
  payload (`#search-results-data`, `data-abs-match`) is emitted whenever ABS
  matching is on. On a flip: badge + counts update in place and a quiet toast
  fires.

## Config

`ABS_URL`, `ABS_TOKEN` (enables the feature), `ABS_LIBRARY_ID` (optional; else
first "book" library), `ABS_CACHE_TTL` (default 900). See README for details.

## Evaluating & tuning — `app/abs_match_spike.py`

Standalone CLI (safe to delete). Imports `abs_match`, never `app.py`.
- `--selftest` — 9 offline regression cases covering the guards; run this after
  any matcher change.
- `python abs_match_spike.py "query" …` — live: pulls the ABS library, scrapes a
  real ABB search (through the container's Tor when run via `docker compose
  exec`), prints each result's best match. `STRONG` rows are what would badge;
  `maybe`/`none` show recall so you can set thresholds.

## History / decisions

Built in phases (see git log on `dev`): deterministic badge → LLM-canonicalized
ownership → ownership-aware series selection → live poll. An earlier draft
considered sending candidate library items to Gemini; that was **rejected** in
favor of the local join so nothing about the library is transmitted.
