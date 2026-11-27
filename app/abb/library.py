"""Audiobookshelf integration: the "in your library" index and everything
computed from it — deterministic badges, LLM-canonical ownership joins, and
the Upgrade Radar quality flags.

The privacy rule this module exists to uphold: the user's library NEVER
leaves the box. ABS is fetched directly (not via Tor — it's the user's own
server) and every ownership decision is a local join against the cached
index. See docs/library-matching.md.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from urllib.parse import urlparse

import requests

from . import matching

log = logging.getLogger("abb.library")

_FRAGMENTED_TRACKS = 8  # >= this many files reads as a per-chapter MP3 rip


def parse_kbps(bitrate_str):
    m = re.search(r"([\d.]+)\s*kbps", bitrate_str or "", re.IGNORECASE)
    return float(m.group(1)) if m else None


def norm_seq(seq):
    s = str(seq).strip()
    return s[:-2] if s.endswith(".0") else s


class AbsLibrary:
    def __init__(self, config):
        self.config = config
        self.enabled = config.abs_enabled
        self._cache = {"items": [], "fetched_at": 0.0}
        self._lock = threading.Lock()

    # --- ABS API ---------------------------------------------------------------
    def _get(self, path, **params):
        r = requests.get(f"{self.config.abs_url}{path}",
                         headers={"Authorization": f"Bearer {self.config.abs_token}"},
                         params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def _pick_library(self):
        if self.config.abs_library_id:
            return self.config.abs_library_id
        libs = self._get("/api/libraries").get("libraries", [])
        for lib in libs:
            if lib.get("mediaType") == "book":
                return lib["id"]
        return libs[0]["id"] if libs else None

    def _load_items(self, library_id):
        items, page, limit = [], 0, 500
        while True:
            data = self._get(f"/api/libraries/{library_id}/items", limit=limit, page=page)
            results = data.get("results", [])
            for it in results:
                media = it.get("media") or {}
                md = media.get("metadata") or {}
                authors = md.get("authors") or []
                author = md.get("authorName") or ", ".join(a.get("name", "") for a in authors)
                series = matching.parse_series_field(md)
                size = media.get("size") or 0
                duration = media.get("duration") or 0
                tracks = media.get("numTracks") or media.get("numAudioFiles") or 0
                items.append({
                    "id": it.get("id") or "",   # stable across refreshes (wanted reopen)
                    "title": md.get("title") or "",
                    # The matcher's identity guard forgives a result's subtitle
                    # only when it matches the owned copy's ("Atomic Habits: An
                    # Easy & Proven Way..." is still the Atomic Habits you own).
                    "subtitle": md.get("subtitle") or "",
                    "author": author,
                    "series": series,
                    "asin": md.get("asin") or "",
                    "isbn": md.get("isbn") or "",
                    "language": md.get("language") or "",
                    "size": size,
                    "duration": duration,
                    "tracks": tracks,
                    # Effective bitrate of the copy on disk; None when unknown.
                    "est_kbps": (size * 8 / duration / 1000) if size and duration else None,
                })
            if len(results) < limit or (data.get("total") and len(items) >= data["total"]):
                break
            page += 1
        return items

    def get_index(self, max_age=None):
        """Cached ABS library items, refreshed once older than the cache TTL.
        `max_age` (seconds) lets a caller demand a fresher snapshot — the
        ownership poll uses a short value so a just-finished download shows up
        quickly, while still fetching ABS at most once per that window
        regardless of how many polls arrive. Keeps the last good snapshot on
        error; returns [] when disabled or not yet loaded."""
        if not self.enabled:
            return []
        ttl = self.config.abs_cache_ttl if max_age is None else min(self.config.abs_cache_ttl, max_age)
        now = time.monotonic()
        with self._lock:
            if self._cache["items"] and now - self._cache["fetched_at"] < ttl:
                return self._cache["items"]
            try:
                lib = self._pick_library()
                items = self._load_items(lib) if lib else []
                self._cache.update(items=items, fetched_at=now)
                log.info("indexed %d Audiobookshelf items", len(items))
            except Exception as e:
                log.warning("Audiobookshelf library fetch failed: %s", e)
                self._cache["fetched_at"] = now  # back off before retrying
            return self._cache["items"]

    def peek_index(self):
        """Whatever is already cached, without ever fetching — for page-load
        nudges that must stay instant and local."""
        return self._cache["items"] if self.enabled else []

    # --- Owned-copy quality ("Upgrade Radar") ------------------------------------
    def quality_flag(self, item):
        """A short human reason when an owned copy looks below par, else None.
        Precision-first, like the matcher: unknown duration/size -> no flag."""
        reasons = []
        kbps, tracks = item.get("est_kbps"), item.get("tracks") or 0
        if kbps and kbps <= self.config.abs_low_kbps:
            reasons.append(f"~{int(round(kbps))} kbps")
        if tracks >= _FRAGMENTED_TRACKS:
            reasons.append(f"{tracks} files")
        return " · ".join(reasons) or None

    def is_upgrade_result(self, book, item):
        """Would this ABB result be a quality upgrade over the owned copy?
        True only when the owned copy is flagged AND the result is M4B AND its
        stated bitrate (when stated) isn't worse than what we already have."""
        if not item or not self.quality_flag(item):
            return False
        if not book.get("is_m4b"):
            return False
        stated = parse_kbps(book.get("bitrate"))
        owned = item.get("est_kbps")
        if stated and owned and stated < owned:
            return False
        return True

    def flagged_items(self):
        """Upgrade Radar rows: every below-par copy, worst first. All local
        arithmetic on the cached index; nothing is transmitted."""
        flagged = []
        for item in self.get_index():
            reason = self.quality_flag(item)
            if not reason:
                continue
            series = next(iter(item.get("series") or []), None)
            size, duration = item.get("size") or 0, item.get("duration") or 0
            flagged.append({
                "title": item["title"],
                "author": item["author"],
                "series": (f"{series[0]} #{norm_seq(series[1])}"
                           if series and series[0] and series[1] is not None else ""),
                "kbps": item.get("est_kbps"),
                "tracks": item.get("tracks") or 0,
                "size_h": f"{size / 1048576:.0f} MB" if size else "?",
                "duration_h": f"{duration / 3600:.1f} h" if duration else "?",
                "reason": reason,
                # Deep link into search; title + author is the query most
                # likely to surface a clean M4B of the same book.
                "query": " ".join(x for x in (item["title"],
                                              item["author"].split(",")[0].strip()) if x),
            })
        flagged.sort(key=lambda f: (f["kbps"] is None, f["kbps"] or 0.0, -f["tracks"]))
        return flagged

    # --- What a badge matched ------------------------------------------------------
    def match_info(self, item):
        """The owned copy a badge points at — shown beside every "in your
        library" / "possibly in your library" flag so the user can check the
        match themselves. Goes only to the user's own browser. `url` opens
        the item in Audiobookshelf when ABS_URL looks reachable from a
        browser (a bare Docker service name wouldn't be)."""
        series = ", ".join(
            f"{n} #{norm_seq(q)}" if q not in (None, "") else n
            for n, q in item.get("series") or [] if n)
        info = {"title": item.get("title") or "", "author": item.get("author") or "",
                "series": series}
        host = urlparse(self.config.abs_url).hostname or ""
        if item.get("id") and ("." in host or host == "localhost"):
            info["url"] = f"{self.config.abs_url}/item/{item['id']}"
        return info

    # --- Deterministic badge (Tier 1) --------------------------------------------
    def annotate_matches(self, books):
        """Tag each result we confidently own with book['library_match'].
        Best-effort: any failure just leaves results unbadged, never breaks a
        search."""
        if not self.enabled:
            return
        try:
            index = self.get_index()
            if not index:
                return
            for book in books:
                raw = book.get("title", "")
                title, author = matching.split_title_author(raw)
                abb = {"raw": raw, "title": title, "author": author,
                       "language": book.get("language", "")}
                tier, _score, item, reason = matching.best_match(abb, index)
                if tier == matching.MAYBE and item and matching.VOLUME_UNKNOWN in reason:
                    # Everything lines up except a volume number the owned
                    # copy doesn't carry: say "possibly", and show the copy.
                    book["library_match"] = dict(self.match_info(item), suspected=True)
                elif tier == matching.STRONG and item:
                    match = self.match_info(item)
                    # Owned, but this result would improve on a below-par copy:
                    # surface it as an invitation rather than a "you have this".
                    if self.is_upgrade_result(book, item):
                        match["upgrade"] = True
                        match["note"] = self.quality_flag(item)
                    book["library_match"] = match
        except Exception as e:
            log.warning("library match annotation failed: %s", e)

    def owns(self, title, author):
        """One-off STRONG check against the index (used by the wanted pipeline)."""
        if not self.enabled:
            return False
        index = self.get_index()
        if not index:
            return False
        tier, _s, _i, _r = matching.best_match(
            {"raw": title, "title": title, "author": author, "language": ""}, index)
        return tier == matching.STRONG

    # --- LLM-canonical ownership join (Tier 2) ------------------------------------
    @staticmethod
    def owned_series_index(index):
        """Map normalized series name -> {normalized seq: owned item}. Carrying
        the item (not just the number) lets upgrade detection see the owned
        copy's quality."""
        m = {}
        for item in index:
            for sname, seq in item.get("series") or []:
                if sname and seq is not None:
                    m.setdefault(matching.normalize(sname), {})[norm_seq(seq)] = item
        return m

    @staticmethod
    def owned_seqs_for(series_name, owned_series):
        """Fuzzy-match a canonical series name to the owned index; merge its
        {seq: item} maps."""
        target = matching.normalize(series_name or "")
        if not target:
            return {}
        seqs = {}
        for name, owned in owned_series.items():
            if name == target or matching.token_set_ratio(target, name) >= 0.8:
                seqs.update(owned)
        return seqs

    def canonical_owned(self, c, index, owned_series):
        """Resolve one canonical identity {title, author, series?, seq?} to the
        owned library item (None when not owned). Series books join exactly on
        (fuzzy series name, seq); everything else falls back to the local
        author-gated matcher. Shared by smart sort and the ownership poll."""
        item, confirmed = self.canonical_lookup(c, index, owned_series)
        return item if confirmed else None

    def canonical_lookup(self, c, index, owned_series):
        """(item, confirmed) for one canonical identity. confirmed=False is a
        *suspected* match: all there but a volume number the owned copy
        doesn't carry (matching.VOLUME_UNKNOWN). (None, False) when nothing."""
        series, seq = c.get("series"), c.get("seq")
        if series and seq is not None:
            hit = self.owned_seqs_for(series, owned_series).get(norm_seq(seq))
            if hit is not None:
                return hit, True
        # The fallback carries the canonical number too, so the matcher's
        # volume guard can refuse a sibling: canonical "Dune" #1 must not
        # resolve to an owned "Dune Messiah" (#2) just because the titles overlap.
        abb = {"raw": c.get("title", ""), "title": c.get("title", ""),
               "author": c.get("author", ""), "language": c.get("language", ""),
               "series": series, "seq": seq}
        tier, _s, item, reason = matching.best_match(abb, index)
        if tier == matching.STRONG:
            return item, True
        if tier == matching.MAYBE and item and matching.VOLUME_UNKNOWN in reason:
            return item, False
        return None, False

    def resolve_ownership(self, ranking, index, results=None):
        """Compute ownership LOCALLY, joining the LLM's canonicalized *public*
        results (ranking['canonical']) to the ABS index. Adds
        ranking['ownership'] = [{id, status, detail}]. Nothing about the
        library is ever sent out — the LLM only supplied clean titles.
        Best-effort; never raises into the request.

        - Series books: exact join on (fuzzy series name, seq).
        - Standalones: the local author-gated matcher.
        - Omnibus/box-set collections: covers ∩ owned seqs -> partial.
        - A match held back only by a volume number the owned copy lacks ->
          status 'suspected'. Every entry carries `match` (the owned copy).
        - Shelf gaps (an entry with no result) you own -> ranking['owned_gaps']
          = [{series: block index, seq}].
        - When `results` (the sanitized rank payload) is given, an owned result
          that would improve on a below-par copy is reported as 'upgrade'
          instead of 'owned', so the UI can invite replacing junk."""
        if not isinstance(ranking, dict) or not index:
            return
        try:
            by_id = {r.get("id"): r for r in results or []}

            def _book_for(rid):
                r = by_id.get(rid) or {}
                fmt = (r.get("format") or "").lower()
                return {"is_m4b": "m4b" in fmt, "bitrate": r.get("bitrate")}

            def _verdict(rid, item, confirmed=True):
                match = self.match_info(item)
                if not confirmed:
                    return {"status": "suspected", "match": match}
                if rid in by_id and self.is_upgrade_result(_book_for(rid), item):
                    return {"status": "upgrade", "detail": self.quality_flag(item),
                            "match": match}
                return {"status": "owned", "match": match}

            owned_series = self.owned_series_index(index)
            ownership = {}
            for c in ranking.get("canonical") or []:
                rid = c.get("id")
                if rid is None:
                    continue
                item, confirmed = self.canonical_lookup(c, index, owned_series)
                if item is not None:
                    ownership[rid] = _verdict(rid, item, confirmed)
            owned_gaps = []
            for bi, block in enumerate(ranking.get("series") or []):
                seqs = self.owned_seqs_for(block.get("label", ""), owned_series)
                # Series members: the shelf already carries each book's number,
                # so we join (series, seq) locally — no per-result canonical
                # card needed. Alternates are the same work as their entry, so
                # they inherit its ownership — each judged for upgrade against
                # its own format.
                for entry in block.get("entries") or []:
                    eseq = entry.get("seq")
                    if eseq is None:
                        continue
                    item, confirmed = seqs.get(norm_seq(eseq)), True
                    if item is None and entry.get("title"):
                        # The owned copy may carry no series metadata (or file
                        # it under another name): fall back to the author-gated
                        # title match, with the number so the volume guard
                        # still refuses a sibling.
                        item, confirmed = self.canonical_lookup(
                            {"title": entry["title"], "author": block.get("author") or "",
                             "series": block.get("label") or "", "seq": eseq},
                            index, owned_series)
                    if item is None:
                        continue
                    rids = [r for r in [entry.get("best_id")] + list(entry.get("alt_ids") or [])
                            if r is not None]
                    if not rids and confirmed:
                        # A gap in the shelf that you already own: nothing to
                        # go looking for (the shelf says so instead of
                        # "not in these results").
                        owned_gaps.append({"series": bi, "seq": eseq})
                    for rid in rids:
                        ownership.setdefault(rid, _verdict(rid, item, confirmed))
                for col in block.get("collections") or []:
                    cid, covers = col.get("id"), col.get("covers") or []
                    if cid is None or not covers:
                        continue
                    owned_n = sum(1 for cv in covers if norm_seq(cv) in seqs)
                    if owned_n == len(covers):
                        ownership[cid] = {"status": "owned"}
                    elif owned_n:
                        ownership[cid] = {"status": "partial",
                                          "detail": f"{owned_n} of {len(covers)}"}
            ranking["ownership"] = [dict(id=rid, **info) for rid, info in ownership.items()]
            ranking["owned_gaps"] = owned_gaps
        except Exception as e:
            log.warning("ownership resolution failed: %s", e)
