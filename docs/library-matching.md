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

## The matcher — `app/abb/matching.py` (pure, stdlib only)

No import side effects (like everything in the package); usable from the app, the tests, and the spike. Key pieces:

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
  - **Volume guard** (`volume_conflict`) — the result is another volume: its
    declared number (`(Series #N)`, `Book N`, or a smart-sort canonical `seq`)
    isn't the owned copy's series number, or the numbers written in the two
    titles disagree (*He Who Fights with Monsters 5* vs *… 4*, or vs the
    unnumbered book 1). Numbers compare numerically (`_norm_num`: `02` = `2`).
    **Unnumbered owned copies** (series numbers are often never filled in on
    the ABS side) get two softenings: when the result's series is known and
    its title is exactly the owned title — a title of its own, not the series
    name — the missing number is no conflict (`_names_same_book`: canonical
    *Regicide*, Completionist Chronicles #2, matches an unnumbered
    *Regicide*); otherwise, if the missing number is the *only* thing in the
    way (`volume_unknown`), the result stays `MAYBE` but carries the
    `VOLUME_UNKNOWN` reason, which the app shows as a **suspected** match.
  - **Identity guard** (`unexplained_words`) — a title word on either side
    that the other can't account for: not in its title, a series name, a
    **subtitle** (the owned copy's ABS subtitle, or either title's
    after-the-colon part — Hardcover titles carry these — *unless* the part
    before the colon is the series name: "Artemis Fowl: The Arctic Incident"
    is book 2, not book 1 with a subtitle), an author credit, or
    volume/genre filler (`_FILLER`: "Book", "Novel", "A LitRPG Adventure"…). This is the
    *Dune* / *Dune Messiah* case: token-set similarity scores a title whose
    words are a subset of the other's at 1.0, and with the author agreeing
    too, owning book 1 once badged every sequel as owned (and marked wanted
    sequels owned before they were ever searched). It costs some recall on
    listings with extra words your library's metadata doesn't explain — by
    design, and Tier 2's LLM-cleaned identities recover most of it.
- `best_match(abb, items)` → best `(tier, score, item, reason)`.
- `candidates(abb, items, k)` → top-k scored items (the cheap "blocker"; kept for
  a possible future LLM-verify path).
- `split_title_author(raw)` — ABB titles are usually `Title - Author`.

**Tuning knobs** are module-level constants at the top: `STRONG_TITLE`,
`MAYBE_TITLE`, `AUTHOR_MIN`, `SERIES_BONUS`. Only `STRONG` ever becomes an
"In your library" badge; a `MAYBE` with the `VOLUME_UNKNOWN` reason becomes
"Possibly in your library".

`parse_series_field(md)` reads an ABS item's series. The library **list**
endpoint always returns minified metadata: no `series` array, only
`seriesName` ("Name #2, Other #1"). Reading just the array once left every
owned book seriesless, so the (series, number) join never fired.

## App integration — `app/abb/library.py` (`AbsLibrary`)

- `get_index(max_age=None)` — fetches + caches the ABS library in memory
  (`ABS_CACHE_TTL`, default 900s). `max_age` forces a fresher snapshot but still
  fetches ABS at most once per that window across all callers (used by the poll
  with `max_age=120`). Keeps the last good snapshot on error. Each item carries
  its ABS `id` (stable across refreshes) and `subtitle` (the identity guard
  forgives a listing's subtitle only when it's the owned copy's).
- `annotate_matches(books)` — Tier 1. Sets `book['library_match'] =
  {title, author, series, url?}` (`match_info`) for `STRONG` matches, plus
  `suspected: True` for a `VOLUME_UNKNOWN` one. Called in the search route.
- `match_info(item)` — the owned copy a flag points at. Every flag shows it
  ("matches “Regicide” by Dakota Krout · The Completionist Chronicles #2") so
  a match can be checked by eye; `url` links the item in ABS when `ABS_URL`
  looks browser-reachable (not a bare Docker service name).
- `resolve_ownership(ranking, index)` — Tier 2. Joins the LLM's `canonical`
  identities to the ABS index locally, adds `ranking['ownership'] =
  [{id, status, detail}]`. Series books join on **(fuzzy series name, seq)**;
  standalones via `best_match`; omnibus `collections` via covers ∩ owned-seqs →
  `partial "N of M"`. Shared helper `canonical_owned`; when the (series, seq)
  join misses, its fuzzy fallback passes the canonical number along so the
  volume guard refuses an owned sibling (canonical *Dune* #1 is not your
  *Dune Messiah* #2). `canonical_lookup` is the same resolution returning
  `(item, confirmed)`; unconfirmed = suspected. Shelf entries whose
  (series, seq) join misses fall back to it by title + the shelf's `author`;
  a shelf **gap** you own is reported in `ranking['owned_gaps']`. Every
  ownership entry carries `match`.
- **`/api/rank`** — when ABS is on, requests the `canonical` block
  (`RankService.rank(..., want_ownership=True)` adds `RANK_CANONICALIZE_INSTRUCTION`
  and the canonical schema block), then runs `resolve_ownership`. The non-ABS
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
| `suspected` | "Possibly in your library" (neutral, dashed) | **nothing changes** — stays ticked, not hidden, not counted, still polled; it informs and shows the copy |
| *(none)* | no badge | normal |

## Upgrade radar (owned-copy quality)

The ABS index also carries each item's `size`, `duration`, `tracks`, and a
computed `est_kbps` (= size×8/duration — ABS doesn't expose bitrate on the
listing, but this effective rate is all we need). `quality_flag(item)` returns
a short reason ("~48 kbps", "27 files") when a copy is at/below `ABS_LOW_KBPS`
(default 63) or has ≥ 8 files; `is_upgrade_result(book, item)` is true when the
owned copy is flagged AND the result is M4B AND its stated bitrate isn't worse.
All local arithmetic; nothing is transmitted.

Surfaces:
- **`/upgrades`** — worst-first table of flagged copies with a "Find better"
  deep link (`/?q=<title author>`; the search route accepts GET `?q=` for this,
  which also makes searches shareable).
- **Deterministic badge** — `annotate_matches` sets
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

## Wanted rows: correcting a wrong "owned"

Precision-first means a wrong positive must be correctable, and on the wanted
list a false "owned" is the costly direction (the book is never searched).
The done shelf therefore has **Search anyway** (`WantedService.reopen`): the
row goes back in the queue and the library item(s) that claimed it are stored
in the row's `owned_ignore` (ABS item ids) — the pre-search check and the
sweep skip those items, while a genuinely different copy landing later still
flips the row. Once per worker, `_verify_owned` re-checks the done shelf with
the current matcher and flags rows it no longer confirms as *Unconfirmed*
(the shelf opens by itself); it never reopens them automatically, since that
could re-download a book you do own.

## Series labels: the one write to Audiobookshelf

Missing series numbers are what force the *suspected* state, and a smart-sort
shelf often knows them. So `resolve_ownership(..., offer_labels=True)` (admins
only — `is_log_admin`, like `/settings`) attaches `match.label =
{item, series, seq}` to an owned copy the shelf places in a series when ABS
has no number for it there (`label_proposal`); owned gaps carry it too. The UI
turns that into **Set series: Name #2** on the match line and, with two or
more on a shelf, **Label N in Audiobookshelf** (one confirm listing every
write).

`POST /api/library/series` → `AbsLibrary.set_series`:

- **Blanks only.** Decided against the item as ABS has it *now* (`GET
  /api/items/:id`), not the cache: a copy already numbered in that series
  (names compared as loosely as the ownership join) answers `already` and
  nothing is sent. A copy in the series with no number keeps ABS's own name.
- **The whole list goes back.** ABS's `PATCH /api/items/:id/media` *replaces*
  a book's series with `metadata.series`, matching by name — so the existing
  series are re-sent with the new one. Sequences must be strings (ABS stores
  anything else as null).
- On success the cached item is updated in place, so the next search joins on
  the number without waiting for the cache TTL.
- A 403 means the ABS token's user lacks update permission; the message says so.

Nothing here changes the privacy rule: the label comes from the public shelf,
and it is written to the user's own server. A whole-library sweep needs
lookups about books that were never searched for, so it lives in Labs as an
opt-in experiment (see architecture.md → Labs). Every write from either path
is recorded (`series_writes`) and can be undone from the sweep's History tab.

## Config

`ABS_URL`, `ABS_TOKEN` (enables the feature), `ABS_LIBRARY_ID` (optional; else
first "book" library), `ABS_CACHE_TTL` (default 900). See README for details.

## Evaluating & tuning — `app/abs_match_spike.py`

Standalone CLI (safe to delete). Imports `abb.matching` only.
- `--selftest` — offline regression cases covering the guards (the same cases,
  and more, live in `tests/test_matching.py`, which CI runs).
- `python abs_match_spike.py "query" …` — live: pulls the ABS library, scrapes a
  real ABB search (through the container's Tor when run via `docker compose
  exec`; it refuses without Tor unless `--direct` is passed), prints each
  result's best match. `STRONG` rows are what would badge; `maybe`/`none` show
  recall so you can set thresholds. After matcher changes, run it on a few
  series you own to see what the guards now hold back.

## History / decisions

Built in phases (see git log on `dev`): deterministic badge → LLM-canonicalized
ownership → ownership-aware series selection → live poll. An earlier draft
considered sending candidate library items to Gemini; that was **rejected** in
favor of the local join so nothing about the library is transmitted. The v3
review ([`v3-review.md`](v3-review.md)) found token-set similarity reading
sequels as book 1 and added the volume + identity guards and the done-shelf
recovery path.
