"""Hardcover wanted list: sync "Want to Read", background-search ABB, rate the
results once, and (optionally) auto-download.

Pipeline per book: wanted -> found -> sent -> owned (or unmatched, re-checked
daily). A successful search is rated by ONE small Gemini call
(RankService.wanted_verdict) with a deterministic fallback, then the row is
SETTLED — found rows are never re-searched or re-rated unless the user forces
that title (the per-row re-check). So the LLM cost is ~one call per wanted
book EVER, not per sync cycle. WANTED_LLM=false keeps it fully deterministic.

Hardcover API notes (docs.hardcover.app): GraphQL at
api.hardcover.app/v1/graphql, Bearer token, 60 req/min, tokens expire Jan 1,
beta. We stay far under the rate limit (a couple of calls per sync), send a
descriptive user-agent per their guidance, and are read-only toward Hardcover.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone

import requests

from . import matching
from .library import parse_kbps
from .scraper import infohash_from_magnet

log = logging.getLogger("abb.wanted")

HARDCOVER_URL = "https://api.hardcover.app/v1/graphql"

# ABB "request" posts describe a book somebody is ASKING for — there's no
# torrent behind them, so they must never become a pick.
ABB_REQUEST_RE = re.compile(r"\(\s*REQ", re.IGNORECASE)

# Self-healing for a starved/blocked Tor exit: after a few consecutive
# unreachable scrapes on the Tor route, ask for a fresh circuit (rate-limited —
# renewal swaps the exit for everyone on the instance) and put the failed rows
# straight back in the queue instead of waiting out the retry TTL.
RENEW_AFTER = 3        # consecutive unreachable searches
RENEW_COOLDOWN = 600   # seconds between automatic renewals
OWNED_SWEEP_TTL = 300  # seconds between local "did it land in ABS yet?" sweeps


def wanted_queries(title, author):
    """Query ladder for one wanted book: MAXIMUM search area first, targeted
    selection later. ABB's search is an AND-ish full-text match, so any extra
    word (subtitle, volume designator, an author who only wrote the comic
    edition) silently zeroes the results. The primary query is therefore the
    bare stem — title cut at subtitle/parenthetical, trailing volume/book/part
    designators stripped — and the verdict stage narrows from the broad haul
    using the full book info. A surname-narrowed query is the fallback, and
    LEADS only for ultra-generic one-word stems ("It"), where the bare term
    matches everything and the right result may not even surface in the
    fetched pages."""
    short = re.split(r"[:(\[]", title)[0].strip() or title.strip()
    # "The Witcher, Vol. 1" -> "The Witcher"; also Book/Part/No. suffixes.
    stem = re.sub(r"[,\s]*\b(?:vol(?:ume)?|book|part|no)\.?\s*\d+\s*$", "",
                  short, flags=re.IGNORECASE).strip(" ,-") or short
    surname = ""
    if author:
        parts = author.split(",")[0].strip().split()
        surname = parts[-1] if parts else ""
    words = [w for w in re.findall(r"[a-z0-9]+", stem.lower())
             if w not in ("the", "a", "an")]
    generic = len(words) <= 1 and (not words or len(words[0]) <= 3)
    narrowed = f"{stem} {surname}" if surname else None
    queries = [narrowed, stem] if (generic and narrowed) else [stem, narrowed]
    seen, out = set(), []
    for q in ((q or "").lower() for q in queries):
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out


def parse_hardcover_wanted(data):
    """Pure: Hardcover user_books payload -> [{hc_id, title, author, slug}]."""
    rows = []
    for ub in data.get("user_books") or []:
        b = ub.get("book") or {}
        if not b.get("title"):
            continue
        authors = [c.get("author", {}).get("name", "")
                   for c in (b.get("contributions") or []) if c.get("author")]
        rows.append({
            "hc_id": b.get("id"),
            "title": b["title"],
            "author": ", ".join(a for a in authors if a),
            "slug": b.get("slug") or "",
        })
    return [r for r in rows if r["hc_id"] is not None]


def candidate_payload(b):
    """The slim, render-ready slice of a matched result stored on the row."""
    return {"title": b.get("title"), "link": b.get("link"),
            "format": b.get("format"), "bitrate": b.get("bitrate"),
            "size": b.get("size"), "language": b.get("language"),
            "is_m4b": bool(b.get("is_m4b"))}


class WantedService:
    def __init__(self, config, store, scraper, library, rank, clients, outbound, tor):
        self.config = config
        self.store = store
        self.scraper = scraper
        self.library = library
        self.rank = rank
        self.clients = clients
        self.outbound = outbound
        self.tor = tor
        self.enabled = config.wanted_enabled
        self.last_sync = 0.0
        self.sync_error = ""     # last sync failure, surfaced on the dashboard
        self._fail_streak = 0
        # None = never happened. Don't use 0.0 with time.monotonic(): its
        # epoch is arbitrary (boot time on Linux), so on a freshly booted
        # machine `monotonic() - 0.0` can sit inside the cooldown window and
        # silently suppress the FIRST renewal/sweep.
        self._last_renew = None
        self._last_owned_sweep = None
        self._stop = threading.Event()  # set when settings rebuild retires this instance

    # --- Hardcover -------------------------------------------------------------
    def _gql(self, query, variables=None):
        token = self.config.hardcover_api_key
        if token and not token.lower().startswith("bearer "):
            token = f"Bearer {token}"
        r = requests.post(HARDCOVER_URL,
                          json={"query": query, "variables": variables or {}},
                          headers={"authorization": token,
                                   "content-type": "application/json",
                                   "user-agent": "audiobookbay-automated (self-hosted wanted-list sync)"},
                          timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            raise RuntimeError(data["errors"][0].get("message", "Hardcover API error"))
        return data.get("data") or {}

    def _fetch_wanted(self):
        me = self._gql("query { me { id } }").get("me")
        uid = (me[0] if isinstance(me, list) and me else me or {}).get("id")
        if not uid:
            raise RuntimeError("Couldn't resolve the Hardcover user id for this token.")
        data = self._gql(
            """query ($uid: Int!) {
                 user_books(where: {user_id: {_eq: $uid}, status_id: {_eq: 1}}) {
                   book { id title slug contributions { author { name } } }
                 }
               }""", {"uid": uid})
        return parse_hardcover_wanted(data)

    def sync_list(self):
        """Refresh the wanted list from Hardcover, upserting new books and
        dropping ones the user removed (their Hardcover list stays the source
        of truth)."""
        wanted = self._fetch_wanted()
        keep = set()
        for w in wanted:
            keep.add(w["hc_id"])
            self.store.wanted_upsert({"hc_id": w["hc_id"], "title": w["title"],
                                      "author": w["author"], "slug": w["slug"]})
        # New rows need a status; don't clobber rows already progressed.
        for row in self.store.wanted_rows():
            if row["hc_id"] in keep and not row.get("status"):
                self.store.wanted_upsert({"hc_id": row["hc_id"], "status": "wanted"})
            if row["hc_id"] < 0:
                keep.add(row["hc_id"])  # manual rows aren't Hardcover's to delete
        self.store.wanted_delete_missing(keep)
        self.last_sync = time.monotonic()
        self.sync_error = ""
        log.info("synced %d wanted books from Hardcover", len(wanted))

    # --- routing for background searches -----------------------------------------
    def _session(self):
        """Session for BACKGROUND searches per WANTED_ROUTE. Manual re-checks
        pass sess=None so they follow the requesting browser's route toggle —
        which doubles as a diagnostic: if re-check finds books the background
        can't, the background route's exit is being blocked."""
        if self.config.wanted_route == "direct":
            return self.outbound.direct_session
        if self.config.wanted_route == "tor":
            return self.outbound.tor_session or self.outbound.direct_session
        return None  # "default": scrape_session() resolves it (server default)

    def _route_is_tor(self):
        if self.config.wanted_route == "tor":
            return True
        if self.config.wanted_route == "direct":
            return False
        return self.config.use_tor and self.tor.available

    def route_label(self):
        if self.config.wanted_route in ("tor", "direct"):
            return self.config.wanted_route.capitalize()
        return ("Tor" if self.config.use_tor and self.tor.available else "Direct") \
            + " (server default)"

    # --- search + pick ---------------------------------------------------------------
    def _rank_deterministic(self, hits):
        """STRONG matches ordered best-first: M4B first, preferred language,
        then stated bitrate. Deterministic counterpart of the AI verdict;
        element 0 is the pick, the rest are the row's expandable alternatives."""
        def key(b):
            return (bool(b.get("is_m4b")), self.config.language_matches(b),
                    parse_kbps(b.get("bitrate")) or 0)
        return sorted(hits, key=key, reverse=True)

    @staticmethod
    def _match_against(books, title, author):
        """STRONG-only matches of scraped ABB results against one clean wanted
        identity. The wanted book acts as a one-item 'library' for the same
        author-gated matcher used everywhere."""
        target = [{"title": title, "author": author, "series": [], "language": ""}]
        hits = []
        for b in books:
            raw = b.get("title", "")
            if ABB_REQUEST_RE.search(raw):
                continue
            rt, ra = matching.split_title_author(raw)
            abb = {"raw": raw, "title": rt, "author": ra,
                   "language": b.get("language", "")}
            tier, _s, _i, _r = matching.best_match(abb, target)
            if tier == matching.STRONG:
                hits.append(b)
        return hits

    def search_one(self, row, sess=None):
        """Search ABB for one wanted book and update its row. Returns the new
        status. A failed scrape (mirror unreachable / blocked exit) is NOT
        "unmatched": the row drops back to 'wanted' with the error in detail
        and retries on the short WANTED_RETRY_TTL instead of the daily
        re-search cadence."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        title, author = row["title"], row.get("author") or ""
        try:
            if self.library.owns(title, author):
                self.store.wanted_upsert({"hc_id": row["hc_id"], "status": "owned",
                                          "searched_at": now})
                return "owned"

            def store_found(ranked, q, verdict_reason):
                best = ranked[0]
                meta = " · ".join(x for x in (best.get("format"), best.get("bitrate"),
                                              best.get("size")) if x and x != "Unknown")
                self.store.wanted_upsert({
                    "hc_id": row["hc_id"], "status": "found",
                    "best_link": best.get("link"), "best_title": best.get("title"),
                    "best_meta": meta, "searched_at": now, "detail": "",
                    "verdict": verdict_reason,
                    "candidates": json.dumps(ranked[:8])})
                log.info("%r: found via %r (%s; %d candidate(s))",
                         title, q, meta, len(ranked))

            considered, tried, ai_reason = 0, 0, ""
            for q in wanted_queries(title, author):
                tried += 1
                books = self.scraper.search(q, max_pages=2, sess=sess)
                if books is None:
                    log.info("%r: ABB unreachable on this route; will retry", title)
                    self.store.wanted_upsert({
                        "hc_id": row["hc_id"], "status": "wanted", "searched_at": now,
                        "detail": "AudioBook Bay didn't respond on the background "
                                  "route — retrying shortly"})
                    return "unreachable"  # row status is 'wanted'; sentinel drives renewal
                considered += len(books)
                usable = [b for b in books if not ABB_REQUEST_RE.search(b.get("title", ""))][:25]
                if not usable:
                    continue
                # The pick comes from ONE small AI verdict over this query's
                # results (rated once, persisted). Deterministic fallback keeps
                # the pipeline working with no key / on API failure.
                verdict = self.rank.wanted_verdict(title, author, [
                    {"id": i, "title": b.get("title"), "format": b.get("format"),
                     "bitrate": b.get("bitrate"), "size": b.get("size"),
                     "language": b.get("language")} for i, b in enumerate(usable)])
                if verdict is not None:
                    if verdict.get("match_found"):
                        idx = [i for i in (verdict.get("ranked") or [])
                               if isinstance(i, int) and 0 <= i < len(usable)]
                        if idx:
                            notes = {n.get("id"): n.get("note")
                                     for n in (verdict.get("notes") or [])}
                            ranked = []
                            for i in idx:
                                c = candidate_payload(usable[i])
                                if notes.get(i):
                                    c["note"] = notes[i]
                                ranked.append(c)
                            store_found(ranked, q, verdict.get("reason") or "")
                            return "found"
                    # The AI looked at these results and says none are this book —
                    # remember why, and try the next query on the ladder.
                    ai_reason = verdict.get("reason") or ai_reason
                    continue
                ranked_det = self._rank_deterministic(self._match_against(usable, title, author))
                if ranked_det:
                    store_found([candidate_payload(b) for b in ranked_det], q, "")
                    return "found"
            # Clear any previous pick — "no longer available" with a stale best
            # match still showing reads as a contradiction.
            detail = f"no confident match ({tried} searches, {considered} results considered)"
            if ai_reason:
                detail += f" — AI: {ai_reason}"
            self.store.wanted_upsert({
                "hc_id": row["hc_id"], "status": "unmatched", "searched_at": now,
                "best_link": None, "best_title": None, "best_meta": None,
                "candidates": None, "verdict": None, "detail": detail})
            log.info("%r: no match (%d searches, %d results)", title, tried, considered)
            return "unmatched"
        except Exception as e:
            log.warning("wanted search failed for %r: %s", title, e)
            self.store.wanted_upsert({"hc_id": row["hc_id"], "status": "wanted",
                                      "searched_at": now, "detail": str(e)})
            return "wanted"

    def mark_sent_by_link(self, link):
        """Called after a successful /send: if that link is a wanted book's
        pick OR any of its stored alternatives, advance the row so the
        dashboard follows the user's action whichever edition they chose."""
        if not (self.enabled and link):
            return
        try:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for row in self.store.wanted_rows():
                if row.get("status") not in ("found", "wanted"):
                    continue
                links = {row.get("best_link")}
                try:
                    links.update(c.get("link")
                                 for c in json.loads(row.get("candidates") or "[]"))
                except ValueError:
                    pass
                if link in links:
                    self.store.wanted_upsert({"hc_id": row["hc_id"], "status": "sent",
                                              "detail": f"sent {now}"})
        except Exception as e:
            log.warning("wanted mark-sent failed: %s", e)

    def auto_send(self, row, m4b_only=True):
        """Auto-download a found match. Hardcover-synced rows keep the
        strictest gate — the pick must be M4B (auto only ever grabs the
        recommended single-file format). Manually-added rows pass
        m4b_only=False: the user explicitly asked for the book and won't be
        coming back to choose, so the best AI-rated pick goes out whatever
        its format (M4B still wins the ranking when one exists). Downloads
        are logged under the requesting user for manual rows.
        When the gate skips or the send fails, the reason lands in the row's
        detail — "auto-download on but nothing happened" must never be a
        mystery on the dashboard."""
        try:
            link, title = row.get("best_link"), row.get("best_title") or row["title"]
            log_user = row.get("added_by") or "hardcover-auto"
            if not (link and self.clients.ok):
                return
            if m4b_only and "m4b" not in (row.get("best_meta") or "").lower():
                self.store.wanted_upsert({"hc_id": row["hc_id"],
                                          "detail": "auto-download skipped — the best "
                                                    "match isn't M4B; send it manually "
                                                    "if you want it"})
                log.info("auto-download skipped for %r: best match isn't M4B", title)
                return
            magnet = self.scraper.extract_magnet_link(link)
            route = self.outbound.route_mode()
            if not magnet:
                self.store.record_download(log_user, title, link, None, "error",
                                           "Failed to extract magnet link", route=route)
                self.store.wanted_upsert({"hc_id": row["hc_id"],
                                          "detail": "auto-download failed — couldn't "
                                                    "extract a magnet link"})
                return
            self.clients.add(magnet, title)
            self.store.record_download(log_user, title, link,
                                       infohash_from_magnet(magnet), "ok",
                                       "Wanted-list auto-download", route=route)
            self.store.wanted_upsert({"hc_id": row["hc_id"], "status": "sent",
                                      "detail": "auto-downloaded"})
            log.info("auto-sent %r", title)
        except Exception as e:
            log.warning("wanted auto-send failed for %r: %s", row.get("title"), e)
            self.store.record_download(row.get("added_by") or "hardcover-auto",
                                       row.get("best_title") or row["title"],
                                       row.get("best_link"), None, "error", str(e),
                                       route=self.outbound.route_mode())
            self.store.wanted_upsert({"hc_id": row["hc_id"],
                                      "detail": f"auto-download failed — {e}"})

    def search_and_autodownload(self, row, sess=None):
        """search_one plus the auto-download step when it's enabled. BOTH
        discovery paths go through this — the background worker and the manual
        per-row re-check — so "auto-download on" means on, regardless of who
        triggered the search that found the book."""
        status = self.search_one(row, sess=sess)
        if status == "found":
            fresh = next((r for r in self.store.wanted_rows()
                          if r["hc_id"] == row["hc_id"]), None)
            if fresh:
                manual = self.is_manual(fresh)
                # Manual adds are fire-and-forget by definition: they always
                # auto-send, best pick regardless of format. Hardcover rows
                # follow the global setting with the strict M4B gate.
                if manual or self.config.wanted_auto_download:
                    self.auto_send(fresh, m4b_only=not manual)
        return status

    # --- user curation -----------------------------------------------------------------
    @staticmethod
    def is_manual(row):
        """Manually-added books carry negative ids — Hardcover ids are always
        positive, so the two populations can never collide and every existing
        mechanism (skip, re-check, sweep, shelves) works on both."""
        return (row.get("hc_id") or 0) < 0

    def add_manual(self, title, author, user):
        """Quick-add a book by hand: check the library first (the whole point
        is telling the user immediately when there's nothing to do), then
        check for a duplicate row, then create a manual row credited to the
        requesting user. Returns (outcome, hc_id): outcome is 'owned',
        'duplicate', or 'added' (hc_id set only when added)."""
        title, author = title.strip(), (author or "").strip()
        norm_t, norm_a = matching.normalize(title), matching.normalize(author)
        rows = self.store.wanted_rows()
        for r in rows:
            if matching.normalize(r.get("title") or "") != norm_t:
                continue
            other_a = matching.normalize(r.get("author") or "")
            if not norm_a or not other_a or norm_a == other_a:
                return "duplicate", None
        if self.library.owns(title, author):
            return "owned", None
        hc_id = min([r["hc_id"] for r in rows if r["hc_id"] < 0], default=0) - 1
        self.store.wanted_upsert({"hc_id": hc_id, "title": title, "author": author,
                                  "slug": "", "status": "wanted", "added_by": user})
        log.info("%r added manually by %s (id %d)", title, user, hc_id)
        return "added", hc_id

    def skip(self, hc_id):
        """Take an unfound book out of the search rotation entirely: no
        background searches, no daily re-checks, until the user allows it
        again. The row stays (Hardcover remains the source of truth for list
        membership) and the ownership sweep still applies — a skipped book
        that later lands in the library flips to owned like anything else."""
        row = next((r for r in self.store.wanted_rows() if r["hc_id"] == hc_id), None)
        if not row:
            return False, "Unknown wanted book."
        if (row.get("status") or "wanted") not in ("wanted", "unmatched"):
            return False, "Only books still being searched can be skipped."
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.store.wanted_upsert({"hc_id": hc_id, "status": "skipped",
                                  "detail": f"skipped {now}"})
        return True, ""

    def unskip(self, hc_id):
        """Put a skipped book back in the queue, due immediately."""
        row = next((r for r in self.store.wanted_rows() if r["hc_id"] == hc_id), None)
        if not row:
            return False, "Unknown wanted book."
        if row.get("status") != "skipped":
            return False, "This book isn't skipped."
        self.store.wanted_upsert({"hc_id": hc_id, "status": "wanted",
                                  "searched_at": None, "detail": ""})
        return True, ""

    def remove_manual(self, hc_id):
        """Delete a manually-added row. Hardcover rows are managed on
        Hardcover — removing them there removes them here on the next sync."""
        row = next((r for r in self.store.wanted_rows() if r["hc_id"] == hc_id), None)
        if not row:
            return False, "Unknown wanted book."
        if not self.is_manual(row):
            return False, ("This book comes from your Hardcover list — remove it "
                           "there and it disappears on the next sync.")
        self.store.wanted_delete(hc_id)
        return True, ""

    # --- scheduling ------------------------------------------------------------------
    def due_rows(self):
        """Rows that need (re)searching: never searched, or past their cadence.
        'wanted' rows (not yet successfully searched, incl. failed scrapes)
        retry on the short WANTED_RETRY_TTL; 'unmatched' rows re-check daily.
        'found' rows are SETTLED — never looked up again unless the user
        forces that title (per-row re-check)."""
        due = []
        now = datetime.now(timezone.utc)
        for row in self.store.wanted_rows():
            status = row.get("status") or "wanted"
            if status in ("found", "sent", "owned", "skipped"):
                continue
            searched = row.get("searched_at")
            if not searched:
                due.append(row)
                continue
            ttl = self.config.wanted_retry_ttl if status == "wanted" \
                else self.config.wanted_research_ttl
            try:
                age = (now - datetime.fromisoformat(searched)).total_seconds()
            except ValueError:
                age = ttl + 1
            if age > ttl:
                due.append(row)
        return due

    def requeue_open(self):
        """Mark every UNRESOLVED row due for a fresh search. Run at boot so a
        restart always sweeps the list — otherwise rows stamped by an older
        (possibly buggier) build sit out their whole retry backoff before
        anything visible happens. Found/sent/owned rows stay put."""
        n = 0
        for row in self.store.wanted_rows():
            if row.get("status") not in ("found", "sent", "owned", "skipped") \
                    and row.get("searched_at"):
                self.store.wanted_upsert({"hc_id": row["hc_id"], "searched_at": None})
                n += 1
        return n

    def requeue_unresolved(self):
        """Sync-now behaviour: make every open row due for a re-search; the
        worker drains them a few per minute. Found rows are settled (rated +
        stored); re-rating one is the per-row re-check's job."""
        for row in self.store.wanted_rows():
            if row.get("status") not in ("found", "sent", "owned", "skipped"):
                self.store.wanted_upsert({"hc_id": row["hc_id"], "searched_at": None})

    def _note_result(self, status):
        self._fail_streak = self._fail_streak + 1 if status == "unreachable" else 0

    def _maybe_renew(self):
        """Renew the Tor circuit when background searches keep failing on Tor.
        Returns True when a renewal happened (so failed rows are requeued)."""
        if self._fail_streak < RENEW_AFTER or not self._route_is_tor():
            return False
        if not self.tor.renewable:
            return False
        if self._last_renew is not None and time.monotonic() - self._last_renew < RENEW_COOLDOWN:
            return False
        ok, message = self.outbound.renew_tor_circuit()
        self._last_renew = time.monotonic()
        self._fail_streak = 0
        log.info("Tor exit looked blocked; circuit renewal: %s", message)
        if ok:
            for row in self.store.wanted_rows():
                if row.get("status") == "wanted" and "didn't respond" in (row.get("detail") or ""):
                    self.store.wanted_upsert({"hc_id": row["hc_id"], "searched_at": None})
        return ok

    def _sweep_owned(self):
        """Flip rows whose book has since landed in the Audiobookshelf
        library — a sent download completing is the main path, but manual
        imports count too. Entirely local: the cached ABS index and the same
        author-gated matcher as everywhere else, so precision-first still
        holds (no confident match, no flip). This is what makes the
        dashboard's promise — sent flips to "In your library" once the
        download arrives — actually true."""
        if not self.library.enabled:
            return
        now = time.monotonic()
        if self._last_owned_sweep is not None and now - self._last_owned_sweep < OWNED_SWEEP_TTL:
            return
        self._last_owned_sweep = now
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for row in self.store.wanted_rows():
            # Everything non-owned is checked — including skipped rows, since
            # a book you own is done regardless of how it got there.
            if row.get("status") == "owned":
                continue
            title, author = row.get("title") or "", row.get("author") or ""
            if title and self.library.owns(title, author):
                self.store.wanted_upsert({"hc_id": row["hc_id"], "status": "owned",
                                          "detail": f"in your library {stamp}"})
                log.info("%r is now in the library; row flipped to owned", title)

    def _worker(self):
        """Background loop: keep the wanted list synced and searched.
        Deliberately gentle — at most 3 ABB searches per minute-tick and a
        couple of Hardcover calls per sync TTL. Exits when stop() is called
        (a settings change rebuilt the service with a fresh worker)."""
        while not self._stop.is_set():
            try:
                if time.monotonic() - self.last_sync > self.config.hardcover_sync_ttl \
                        or self.last_sync == 0:
                    try:
                        self.sync_list()
                    except Exception as e:
                        self.sync_error = str(e)
                        log.warning("Hardcover sync failed: %s", e)
                self._sweep_owned()  # local only — needs no route, runs regardless of Tor
                if self.tor.status() in ("ready", "unavailable"):  # don't scrape mid-bootstrap
                    due = self.due_rows()
                    if due:
                        log.info("%d book(s) due; searching up to 3 via %s",
                                 len(due), self.route_label())
                    for row in due[:3]:
                        status = self.search_and_autodownload(row, sess=self._session())
                        self._note_result(status)
                        if status == "unreachable":
                            break  # this route is down right now; stop burning the tick
                    self._maybe_renew()
            except Exception as e:
                log.warning("wanted worker tick failed: %s", e)
            self._stop.wait(60)

    def stop(self):
        self._stop.set()

    def start(self):
        if not self.enabled:
            log.info("Hardcover wanted list disabled (no HARDCOVER_API_KEY)")
            return
        try:
            requeued = self.requeue_open()
        except Exception as e:
            requeued = 0
            log.warning("wanted requeue at boot failed: %s", e)
        threading.Thread(target=self._worker, daemon=True, name="wanted-worker").start()
        log.info("Hardcover wanted list enabled%s%s",
                 ", auto-download ON (M4B-only)" if self.config.wanted_auto_download
                 else ", dashboard only",
                 f"; requeued {requeued} open books for a fresh sweep" if requeued else "")
