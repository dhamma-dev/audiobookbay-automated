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

import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from . import matching
from .library import parse_kbps
from .outbound import TorUnavailable
from .scraper import infohash_from_magnet
from .smart_sort import VERDICT_PROMPT_REV

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
OWNED_SWEEP_TTL = 120   # worker "did it land in ABS yet?" sweeps (also caps index age)
CACHED_SWEEP_TTL = 30   # page-load sweeps against the already-cached index

# Detail prefix for owned rows the current matcher no longer confirms. They
# stay on the done shelf (reopening automatically could re-download a book you
# do own); the dashboard flags them so the user can decide with "Search anyway".
UNCONFIRMED = "unconfirmed"

# Verdict memory. A "no match" verdict means the AI judged every listing it
# was shown and none is the book, so a background re-check only needs to ask
# about listings it hasn't judged yet — the daily check of a book that isn't
# on ABB used to re-send the same listings every day (and on every restart,
# Settings save and Sync now). Remembered judgments are retired after
# VERDICT_MEMORY_DAYS, and whenever the book, model, prompt or language
# preference changes, so a mistaken "no" can't stick; the per-row re-check
# always asks afresh.
VERDICT_MEMORY_DAYS = 30
VERDICT_MEMORY_MAX = 400   # judged-listing keys kept per book (oldest dropped)


def library_identity(item):
    """A key for one library item that survives index refreshes: its
    Audiobookshelf id, else (normalized) title + author."""
    return item.get("id") or (f"{matching.normalize(item.get('title') or '')}"
                              f"|{matching.normalize(item.get('author') or '')}")


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


def listing_key(b):
    """One listing exactly as the AI saw it: its ABB post plus every field the
    verdict reads. An edited post (a sample replaced by the full book) makes
    a new key, so it is judged again."""
    fields = [urlparse(b.get("link") or "").path]
    fields += [str(b.get(f) or "") for f in ("title", "format", "bitrate", "size", "language")]
    return hashlib.sha1("\x1f".join(fields).encode()).hexdigest()[:16]


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
        self._last_cached_sweep = None
        self._owned_verified = False   # the done shelf is re-checked once per worker
        self._paused_logged = None     # last Tor pause reason logged (log once per change)
        self._stop = threading.Event()  # set when settings rebuild retires this instance
        # Rows with a search currently running. A quick-add's inline search
        # and the worker tick can otherwise race for the same fresh row —
        # observed live: double scrape, double Gemini verdict, and (had it
        # matched) potentially a double send.
        self._inflight = set()
        self._inflight_lock = threading.Lock()

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
    def _intended_route(self):
        """Where BACKGROUND searches go: WANTED_ROUTE, else the server default
        (USE_TOR). Intent only — whether Tor is up is background_paused()'s
        question, and the answer is never "go Direct instead"."""
        if self.config.wanted_route in ("tor", "direct"):
            return self.config.wanted_route
        return "tor" if self.config.use_tor else "direct"

    def _session(self):
        """Session for BACKGROUND searches (raises TorUnavailable rather than
        falling back to Direct). Manual re-checks pass sess=None so they
        follow the requesting browser's route toggle — which doubles as a
        diagnostic: if re-check finds books the background can't, the
        background route's exit is being blocked."""
        return self.outbound.session_for(self._intended_route())

    def background_paused(self):
        """Why background searches are on hold, or None when they can run:
        'starting' / 'unavailable' when their route is Tor and Tor isn't up.
        Shown on the dashboard — a paused queue must never look idle."""
        if self._intended_route() == "tor" and not self.outbound.tor_ready():
            return self.tor.status()
        return None

    def _route_is_tor(self):
        return self._intended_route() == "tor"

    def auto_policy_label(self):
        """The universal auto-download requirements, as shown to humans."""
        label = "M4B only" if self.config.wanted_auto_format == "m4b" else "any format"
        if self.config.wanted_auto_min_kbps:
            label += f", ≥ {self.config.wanted_auto_min_kbps:g} kbps"
        return label

    def route_label(self):
        label = self._intended_route().capitalize()
        return label if self.config.wanted_route in ("tor", "direct") \
            else label + " (server default)"

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

    # --- verdict memory ----------------------------------------------------------------
    def _llm_active(self):
        """Will the AI verdict be asked? The memory only applies then — with
        it off, the deterministic matcher runs exactly as it always has."""
        return bool(getattr(self.rank, "enabled", False) and self.config.wanted_llm)

    def _verdict_stamp(self, title, author):
        """Everything a verdict depends on besides the listings: the book, the
        model, the prompt and the language preference."""
        parts = (matching.normalize(title), matching.normalize(author), self.config.rank_model,
                 VERDICT_PROMPT_REV, (self.config.preferred_language or "").lower())
        return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:12]

    @staticmethod
    def _verdict_memory(row, stamp):
        """What the AI already judged for this book, if that still applies
        (same stamp, younger than VERDICT_MEMORY_DAYS); else None."""
        try:
            memory = json.loads(row.get("verdict_cache") or "")
            since = datetime.fromisoformat(memory["since"])
        except (ValueError, KeyError, TypeError):
            return None
        if memory.get("stamp") != stamp or \
                (datetime.now(timezone.utc) - since).days >= VERDICT_MEMORY_DAYS:
            return None
        return memory

    @staticmethod
    def _remembered(stamp, memory, new_keys, reason, now):
        """The memory to store once a search got verdicts: its judged listings
        added (newest last, capped) to what still applied."""
        keys = list(memory["keys"]) if memory else []
        known = set(keys)
        keys += [k for k in new_keys if k not in known]
        return json.dumps({"stamp": stamp, "since": memory["since"] if memory else now,
                           "updated": now, "keys": keys[-VERDICT_MEMORY_MAX:],
                           "reason": reason or (memory or {}).get("reason", "")})

    def search_one(self, row, sess=None, fresh=False):
        """Search ABB for one wanted book and update its row. Returns the new
        status. A failed scrape (mirror unreachable / blocked exit) is NOT
        "unmatched": the row drops back to 'wanted' with the error in detail
        and retries on the short WANTED_RETRY_TTL instead of the daily
        re-search cadence. The AI is only asked about listings it hasn't
        already judged for this book (see VERDICT_MEMORY_DAYS) — unless
        `fresh`, which the per-row re-check sets: a human asking gets a fresh
        look at everything."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        title, author = row["title"], row.get("author") or ""
        with self._inflight_lock:
            if row["hc_id"] in self._inflight:
                log.info("%r: a search is already in flight; skipping the duplicate", title)
                return "in-flight"
            self._inflight.add(row["hc_id"])
        try:
            if self.library.enabled and self._owned_match(row, self.library.get_index()):
                self.store.wanted_upsert({"hc_id": row["hc_id"], "status": "owned",
                                          "searched_at": now})
                return "owned"

            # Verdict memory, only when the AI will actually be asked.
            use_memory = self._llm_active()
            stamp = self._verdict_stamp(title, author) if use_memory else None
            memory = None if fresh or not use_memory else self._verdict_memory(row, stamp)
            judged = set(memory["keys"]) if memory else set()
            new_keys, calls, skipped = [], 0, 0

            def remember(update):
                """Fold what the AI judged in this search into the row update."""
                if new_keys:
                    update["verdict_cache"] = self._remembered(stamp, memory, new_keys,
                                                               ai_reason, now)
                return update

            def store_found(ranked, q, verdict_reason):
                best = ranked[0]
                meta = " · ".join(x for x in (best.get("format"), best.get("bitrate"),
                                              best.get("size")) if x and x != "Unknown")
                self.store.wanted_upsert(remember({
                    "hc_id": row["hc_id"], "status": "found",
                    "best_link": best.get("link"), "best_title": best.get("title"),
                    "best_meta": meta, "searched_at": now, "detail": "",
                    "verdict": verdict_reason,
                    "candidates": json.dumps(ranked[:8])}))
                log.info("%r: found via %r (%s; %d candidate(s))",
                         title, q, meta, len(ranked))

            considered, tried, ai_reason = 0, 0, ""
            for q in wanted_queries(title, author):
                tried += 1
                books = self.scraper.search(q, max_pages=2, sess=sess)
                if books is None:
                    log.info("%r: ABB unreachable on this route; will retry", title)
                    # remember(): verdicts from earlier on this ladder still stand.
                    self.store.wanted_upsert(remember({
                        "hc_id": row["hc_id"], "status": "wanted", "searched_at": now,
                        "detail": "AudioBook Bay didn't respond on the background "
                                  "route — retrying shortly"}))
                    return "unreachable"  # row status is 'wanted'; sentinel drives renewal
                considered += len(books)
                usable = [b for b in books if not ABB_REQUEST_RE.search(b.get("title", ""))]
                if use_memory:
                    # Only listings the AI hasn't judged for this book, in an
                    # earlier check or earlier on this ladder: every one it saw
                    # was ruled out, so those can't change the answer. (This
                    # also lets results past the first 25 get a look over time.)
                    unseen, keys = [], set()
                    for b in usable:
                        k = listing_key(b)
                        if k not in judged and k not in keys:
                            keys.add(k)
                            unseen.append(b)
                    if usable and not unseen:
                        skipped += 1
                        continue
                    usable = unseen
                usable = usable[:25]
                if not usable:
                    continue
                # The pick comes from ONE small AI verdict over this query's
                # results (rated once, persisted). Deterministic fallback keeps
                # the pipeline working with no key / on API failure — and, with
                # the memory on, it never sees a listing the AI already ruled out.
                calls += 1
                verdict = self.rank.wanted_verdict(title, author, [
                    {"id": i, "title": b.get("title"), "format": b.get("format"),
                     "bitrate": b.get("bitrate"), "size": b.get("size"),
                     "language": b.get("language")} for i, b in enumerate(usable)])
                if verdict is not None:
                    idx = []
                    if verdict.get("match_found"):
                        idx = [i for i in (verdict.get("ranked") or [])
                               if isinstance(i, int) and 0 <= i < len(usable)]
                    if use_memory and (idx or not verdict.get("match_found")):
                        # A coherent verdict judged every listing it was shown.
                        # ("Match" with no valid pick isn't one; don't keep it.)
                        batch = [listing_key(b) for b in usable]
                        judged.update(batch)
                        new_keys.extend(batch)
                    if verdict.get("match_found"):
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
            if skipped and not calls and memory:
                # Nothing new since the AI last looked: say so, with its reason.
                detail += ("; nothing new since the AI last looked "
                           f"({memory.get('updated', memory['since'])[:10]})")
            # The remembered reason only when nothing new was asked; after a new
            # verdict it could describe listings that were not this batch.
            reason = ai_reason or ("" if calls else (memory or {}).get("reason", ""))
            if reason:
                detail += f" — AI: {reason}"
            self.store.wanted_upsert(remember({
                "hc_id": row["hc_id"], "status": "unmatched", "searched_at": now,
                "best_link": None, "best_title": None, "best_meta": None,
                "candidates": None, "verdict": None, "detail": detail}))
            log.info("%r: no match (%d searches, %d results; AI asked %d time(s), "
                     "%d search(es) skipped: nothing new since the last verdict)",
                     title, tried, considered, calls, skipped)
            return "unmatched"
        except TorUnavailable:
            # The route is Tor and Tor isn't up, so nothing went out. That's
            # not a failed search: leave the row untouched (a new one stays
            # due) so it runs the moment Tor is back, with no retry backoff.
            log.info("%r: waiting for Tor (never sent direct)", title)
            return "tor-unavailable"
        except Exception as e:
            log.warning("wanted search failed for %r: %s", title, e)
            self.store.wanted_upsert({"hc_id": row["hc_id"], "status": "wanted",
                                      "searched_at": now, "detail": str(e)})
            return "wanted"
        finally:
            with self._inflight_lock:
                self._inflight.discard(row["hc_id"])

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

    def _auto_gate_reason(self, row):
        """Why the server's auto-download requirements block this pick, or
        None when it passes. ONE universal policy (configurable in Settings)
        — Hardcover-synced and app-added books are judged identically:

        - WANTED_AUTO_FORMAT "m4b": only the single-file format auto-sends.
        - WANTED_AUTO_MIN_KBPS: a pick whose STATED bitrate is below the
          minimum is held; listings that don't state one pass (blocking on
          missing metadata would strand too many legitimate picks)."""
        meta = row.get("best_meta") or ""
        if self.config.wanted_auto_format == "m4b" and "m4b" not in meta.lower():
            return "isn't M4B"
        if self.config.wanted_auto_min_kbps:
            kbps = parse_kbps(meta)
            if kbps is not None and kbps < self.config.wanted_auto_min_kbps:
                return (f"{kbps:g} kbps is below the "
                        f"{self.config.wanted_auto_min_kbps:g} kbps minimum")
        return None

    def auto_send(self, row, sess=None):
        """Auto-download a found match, subject to the universal requirements
        (_auto_gate_reason). Downloads are logged under the requesting user
        for app-added rows, "hardcover-auto" otherwise. When the gate holds
        or the send fails, the reason lands in the row's detail —
        "auto-download on but nothing happened" must never be a mystery on
        the dashboard. `sess` is the session the search used: the detail page
        goes out the same way (it once took the server default instead, so
        WANTED_ROUTE=tor could fetch it direct)."""
        route = self.outbound.route_of(sess) if sess is not None else self.outbound.route_mode()
        try:
            link, title = row.get("best_link"), row.get("best_title") or row["title"]
            log_user = row.get("added_by") or "hardcover-auto"
            if not (link and self.clients.ok):
                return
            reason = self._auto_gate_reason(row)
            if reason:
                self.store.wanted_upsert({"hc_id": row["hc_id"],
                                          "detail": f"auto-download skipped — the best "
                                                    f"match {reason} (server policy; "
                                                    f"send it manually if you want it)"})
                log.info("auto-download skipped for %r: best match %s", title, reason)
                return
            magnet = self.scraper.extract_magnet_link(link, sess=sess, title=title)
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
            update = {"hc_id": row["hc_id"], "status": "sent",
                      "detail": "auto-downloaded"}
            if not row.get("author"):
                # Borrow the author from the listing we just sent, so the
                # ownership sweep can author-gate this row later.
                _bt, author = matching.split_title_author(title)
                if author:
                    update["author"] = author
            self.store.wanted_upsert(update)
            log.info("auto-sent %r", title)
        except Exception as e:
            log.warning("wanted auto-send failed for %r: %s", row.get("title"), e)
            self.store.record_download(row.get("added_by") or "hardcover-auto",
                                       row.get("best_title") or row["title"],
                                       row.get("best_link"), None, "error", str(e),
                                       route=route)
            self.store.wanted_upsert({"hc_id": row["hc_id"],
                                      "detail": f"auto-download failed — {e}"})

    def search_and_autodownload(self, row, sess=None, fresh=False):
        """search_one plus the auto-download step when it's enabled. BOTH
        discovery paths go through this — the background worker and the manual
        per-row re-check — so "auto-download on" means on, regardless of who
        triggered the search that found the book. `fresh` (the re-check)
        ignores the verdict memory."""
        status = self.search_one(row, sess=sess, fresh=fresh)
        if status == "found":
            fresh = next((r for r in self.store.wanted_rows()
                          if r["hc_id"] == row["hc_id"]), None)
            if fresh:
                # Manual adds are fire-and-forget by definition, so they
                # always attempt auto-send; Hardcover rows follow the master
                # switch. The QUALITY requirements are one universal server
                # policy either way (see _auto_gate_reason).
                if self.is_manual(fresh) or self.config.wanted_auto_download:
                    self.auto_send(fresh, sess=sess)
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

    def reopen(self, hc_id, user):
        """"Not actually in my library": put an owned row back in the search
        queue and remember which library item(s) claimed it, so neither the
        sweep nor the pre-search check re-flips it on that same match. A
        different copy landing later still flips it — only the rejected match
        is ignored, the matcher stays in charge."""
        row = next((r for r in self.store.wanted_rows() if r["hc_id"] == hc_id), None)
        if not row:
            return False, "Unknown wanted book."
        if row.get("status") != "owned":
            return False, "Only books marked as in your library can be reopened."
        ignored = self._ignored(row)
        if self.library.enabled:
            index = self.library.get_index()
            for _ in range(5):  # everything that matches now, not just the best
                item = self._owned_match({**row, "owned_ignore": json.dumps(sorted(ignored))},
                                         index)
                if item is None:
                    break
                ignored.add(library_identity(item))
        # verdict_cache cleared too: reopening says something was wrong about
        # this book, so it gets a fresh AI look rather than remembered verdicts.
        self.store.wanted_upsert({"hc_id": hc_id, "status": "wanted", "searched_at": None,
                                  "owned_ignore": json.dumps(sorted(ignored)) if ignored else None,
                                  "verdict_cache": None,
                                  "detail": f"reopened by {user} — not in the library after all"})
        log.info("%r reopened by %s (ignoring %d library match(es))",
                 row.get("title"), user, len(ignored))
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

    # --- ownership ---------------------------------------------------------------------
    @staticmethod
    def _row_identity(row):
        """(title, author) to match a row against the library. Rows added
        without an author borrow one from the sent pick's listing title — we
        know exactly which listing went out, so "Treasure Island" sent as
        "Treasure Island - Robert Louis Stevenson" can still author-gate."""
        title, author = row.get("title") or "", row.get("author") or ""
        if not author and row.get("best_title"):
            _bt, author = matching.split_title_author(row["best_title"])
        return title, author

    @staticmethod
    def _ignored(row):
        """Library identities the user said are NOT this book (reopen)."""
        try:
            return set(json.loads(row.get("owned_ignore") or "[]"))
        except ValueError:
            return set()

    def _owned_match(self, row, index):
        """The library item this row confidently IS, else None: the same
        author-gated matcher as everywhere, minus any matches the user
        rejected. Shared by the pre-search check, the sweep, and reopen."""
        title, author = self._row_identity(row)
        ignored = self._ignored(row)
        if ignored:
            index = [it for it in index if library_identity(it) not in ignored]
        if not title or not index:
            return None
        tier, _s, item, _r = matching.best_match(
            {"raw": title, "title": title, "author": author, "language": ""}, index)
        return item if tier == matching.STRONG else None

    def _flip_owned(self, index):
        """Flip rows whose book has since landed in the Audiobookshelf
        library — a sent download completing is the main path, but manual
        imports count too. Precision-first like everywhere else: no confident
        match (and never one the user rejected), no flip."""
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for row in self.store.wanted_rows():
            # Everything non-owned is checked — including skipped rows, since
            # a book you own is done regardless of how it got there.
            if row.get("status") == "owned":
                continue
            if self._owned_match(row, index) is not None:
                self.store.wanted_upsert({"hc_id": row["hc_id"], "status": "owned",
                                          "detail": f"in your library {stamp}"})
                log.info("%r is now in the library; row flipped to owned", row.get("title"))

    def _verify_owned(self, index):
        """Once per worker: re-check the done shelf with the CURRENT matcher.
        Rows it no longer confirms are flagged, never reopened automatically
        — reopening could re-download a book you do own, so that call stays
        with the user ("Search anyway"). This is how rows an older, looser
        matcher filed as owned (a sequel read as book 1) come to light."""
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for row in self.store.wanted_rows():
            if row.get("status") != "owned":
                continue
            flagged = (row.get("detail") or "").startswith(UNCONFIRMED)
            confirmed = self._owned_match(row, index) is not None
            if not confirmed and not flagged:
                self.store.wanted_upsert({
                    "hc_id": row["hc_id"],
                    "detail": f"{UNCONFIRMED} — no library item confidently matches this "
                              "book any more; if you don't have it, use “Search anyway”"})
                log.info("%r is marked owned but no longer matches the library; flagged",
                         row.get("title"))
            elif confirmed and flagged:
                self.store.wanted_upsert({"hc_id": row["hc_id"],
                                          "detail": f"in your library {stamp}"})

    def _sweep_owned(self, force=False, max_age=None):
        """The worker's sweep: throttled, and it demands an index at most
        OWNED_SWEEP_TTL old — a fresh arrival must not wait out the full
        15-minute ABS cache on top of the sweep cadence (observed live: the
        search page's badge flipped while the wanted row sat on "Sent")."""
        if not self.library.enabled:
            return
        now = time.monotonic()
        if not force and self._last_owned_sweep is not None \
                and now - self._last_owned_sweep < OWNED_SWEEP_TTL:
            return
        self._last_owned_sweep = now
        index = self.library.get_index(
            max_age=OWNED_SWEEP_TTL if max_age is None else max_age)
        if index:
            self._flip_owned(index)
            if not self._owned_verified:
                self._owned_verified = True
                self._verify_owned(index)

    def sweep_owned_now(self):
        """Sync now's companion: an immediate sweep against a fresh index, so
        the user's refresh-everything button also answers "did my downloads
        land yet?"."""
        self._sweep_owned(force=True, max_age=30)

    def sweep_owned_cached(self):
        """Page-load nudge: flip owned rows against whatever index is already
        in memory. Pure local matching, never a network call — the wanted
        page stays fast but reflects anything another page (a search, the
        ownership poll) has already learned."""
        if not self.library.enabled:
            return
        now = time.monotonic()
        if self._last_cached_sweep is not None \
                and now - self._last_cached_sweep < CACHED_SWEEP_TTL:
            return
        self._last_cached_sweep = now
        index = self.library.peek_index()
        if index:
            self._flip_owned(index)

    def _tick(self):
        """One worker pass: sync when due, sweep ownership, then up to 3
        searches — unless their route is Tor and Tor isn't up, in which case
        the searches wait (never Direct instead) and the dashboard says so."""
        if time.monotonic() - self.last_sync > self.config.hardcover_sync_ttl \
                or self.last_sync == 0:
            try:
                self.sync_list()
            except Exception as e:
                self.sync_error = str(e)
                log.warning("Hardcover sync failed: %s", e)
        self._sweep_owned()  # local only — needs no route, runs regardless of Tor
        paused = self.background_paused()
        if paused:
            if paused != self._paused_logged:
                log.info("background searches paused: Tor is %s and their route is Tor "
                         "(set WANTED_ROUTE=direct to search without it)", paused)
                self._paused_logged = paused
            return
        self._paused_logged = None
        due = self.due_rows()
        if due:
            log.info("%d book(s) due; searching up to 3 via %s", len(due), self.route_label())
        for row in due[:3]:
            if self._stop.is_set():
                break  # retired by a settings save: leave the rest to the new worker
            status = self.search_and_autodownload(row, sess=self._session())
            if status == "in-flight":
                continue  # someone else is already searching this row
            self._note_result(status)
            if status in ("unreachable", "tor-unavailable"):
                break  # this route is down right now; stop burning the tick
        self._maybe_renew()

    def _worker(self):
        """Background loop: keep the wanted list synced and searched.
        Deliberately gentle — at most 3 ABB searches per minute-tick and a
        couple of Hardcover calls per sync TTL. Exits when stop() is called
        (a settings change rebuilt the service with a fresh worker)."""
        while not self._stop.is_set():
            try:
                self._tick()
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
        log.info("Hardcover wanted list enabled; auto-download %s, policy: %s%s",
                 "ON" if self.config.wanted_auto_download
                 else "off for Hardcover rows (app adds always auto)",
                 self.auto_policy_label(),
                 f"; requeued {requeued} open books for a fresh sweep" if requeued else "")
