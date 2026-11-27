"""Series gap fill: look for the books a smart-sort shelf is missing.

A series-name search often misses individual volumes — ABB uploads are
labelled by book title alone, or by author, or just badly. The shelf already
knows what is missing (the model names each gap), so this runs a few gentle,
targeted searches for those books only, on request:

  1. Per gap: skip it if the library already has it; otherwise the same
     broad-first query ladder the wanted list uses (bare title, then title +
     author surname).
  2. One shared author search for whatever is still missing, checked against
     every remaining gap at once.

Listings are matched by the same author-gated matcher as everything else
(the gap acts as a one-item "library" carrying its series number, so a
sibling volume is refused). When that finds nothing, the small wanted-list
AI verdict gets a look — that is what catches the badly labelled uploads.
It sees only public listing metadata, like smart sort.

Nothing here is automatic: each search is a real request to the mirror (over
Tor on that route, and failing closed like every other scrape), so it only
runs when the user asks, with small page counts and a per-request gap cap.
"""

from __future__ import annotations

import logging
import re

from . import matching
from .library import norm_seq, parse_kbps
from .wanted import ABB_REQUEST_RE, wanted_queries

log = logging.getLogger("abb.gapfill")

MAX_GAPS = 8          # gaps handled per request (the client asks one at a time)
TITLE_PAGES = 2       # result pages per title query
AUTHOR_PAGES = 3      # result pages for the one shared author search
VERDICT_MAX = 25      # listings shown to the AI per verdict
KEEP = 4              # editions returned per filled gap (pick + alternatives)


def clean_gaps(raw):
    """Validated [{seq, title}] from client JSON, capped at MAX_GAPS."""
    gaps = []
    for g in raw if isinstance(raw, list) else []:
        if not isinstance(g, dict):
            continue
        title = str(g.get("title") or "").strip()[:300]
        seq = g.get("seq")
        if not title or isinstance(seq, bool) or not isinstance(seq, (int, float, str)):
            continue
        seq = norm_seq(seq)[:12]
        if not re.fullmatch(r"\d+(?:\.\d+)?", seq):
            continue
        gaps.append({"seq": seq, "title": title})
    return gaps[:MAX_GAPS]


def author_query(author):
    """The broad author search: the first credited author's full name."""
    first = re.split(r"[,;/&]| and ", author or "")[0]
    return " ".join(re.findall(r"[a-z0-9']+", first.lower()))


def _usable(books, seen):
    """Real listings (no request posts) not already considered for this gap."""
    out = []
    for b in books:
        link = b.get("link")
        if not link or link in seen or ABB_REQUEST_RE.search(b.get("title", "")):
            continue
        seen.add(link)
        out.append(b)
    return out


def _strong(books, label, author, gap):
    """Listings the matcher confirms are this gap's book."""
    target = {"title": gap["title"], "subtitle": "", "author": author,
              "series": [(label, gap["seq"])] if label else [], "language": ""}
    hits = []
    for b in books:
        raw = b.get("title", "")
        title, listed_author = matching.split_title_author(raw)
        abb = {"raw": raw, "title": title, "author": listed_author,
               "language": b.get("language", "")}
        if matching.score_pair(abb, target)[0] == matching.STRONG:
            hits.append(b)
    return hits


def _plausible(book, label, gap):
    """Cheap pre-filter for the author haul, so the AI is only shown listings
    that could be this gap: they share a title word, or carry the series name
    with this book's number ("Completionist Chronicles 2" — no title at all,
    exactly the poorly labelled upload this feature is for)."""
    raw = book.get("title", "")
    words = matching.tokens(raw)
    series_words = matching.tokens(label)
    if (matching.tokens(gap["title"]) - series_words) & words:
        return True
    numbers = {norm_seq(f"{float(n):g}") for n in re.findall(r"\d+(?:\.\d+)?", raw)}
    return bool(series_words) and series_words <= words and gap["seq"] in numbers


def _pick(s, books, label, author, gap):
    """Best-first editions of this gap's book among `books`, or []. The
    matcher decides when it can; the AI verdict is the fallback."""
    hits = _strong(books, label, author, gap)
    if hits:
        hits.sort(key=lambda b: (s.config.language_matches(b), bool(b.get("is_m4b")),
                                 parse_kbps(b.get("bitrate")) or 0), reverse=True)
        return hits[:KEEP]
    shown = books[:VERDICT_MAX]
    wanted_title = f"{gap['title']} ({label}, Book {gap['seq']})" if label else gap["title"]
    verdict = s.rank.wanted_verdict(wanted_title, author, [
        {"id": i, "title": b.get("title"), "format": b.get("format"),
         "bitrate": b.get("bitrate"), "size": b.get("size"),
         "language": b.get("language")} for i, b in enumerate(shown)])
    if not verdict or not verdict.get("match_found"):
        return []
    notes = {n.get("id"): n.get("note") for n in verdict.get("notes") or []}
    picked, used = [], set()
    for i in verdict.get("ranked") or []:
        if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(shown) and i not in used:
            used.add(i)
            picked.append(dict(shown[i], note=notes.get(i) or ""))
    return picked[:KEEP]


def _owned(s, label, author, gap):
    if not s.library.enabled:
        return False
    index = s.library.get_index()
    if not index:
        return False
    identity = {"title": gap["title"], "author": author, "series": label, "seq": gap["seq"]}
    return s.library.canonical_owned(identity, index, s.library.owned_series_index(index)) is not None


def fill_by_title(s, label, author, gaps, sess=None):
    """Step 1, per gap. Returns [{seq, status, books}] with status 'owned',
    'found', 'missing' or 'unreachable' (the mirror didn't answer — stop and
    let the user retry rather than calling the book missing). TorUnavailable
    propagates: the route fails closed."""
    out = []
    for gap in gaps:
        if _owned(s, label, author, gap):
            out.append({"seq": gap["seq"], "status": "owned", "books": []})
            continue
        status, books, seen = "missing", [], set()
        for q in wanted_queries(gap["title"], author):
            haul = s.scraper.search(q, max_pages=TITLE_PAGES, sess=sess)
            if haul is None:
                status = "unreachable"
                break
            books = _pick(s, _usable(haul, seen), label, author, gap)
            if books:
                status = "found"
                break
        log.info("%r #%s %r: %s", label, gap["seq"], gap["title"], status)
        out.append({"seq": gap["seq"], "status": status, "books": books})
        if status == "unreachable":
            break
    return out


def fill_by_author(s, label, author, gaps, sess=None):
    """Step 2: ONE broad author search, checked against every remaining gap.
    Same result shape as fill_by_title; a listing fills at most one gap."""
    query = author_query(author)
    if not query or not gaps:
        return [{"seq": g["seq"], "status": "missing", "books": []} for g in gaps]
    haul = s.scraper.search(query, max_pages=AUTHOR_PAGES, sess=sess)
    if haul is None:
        return [{"seq": g["seq"], "status": "unreachable", "books": []} for g in gaps]
    usable, taken, out = _usable(haul, set()), set(), []
    for gap in gaps:
        pool = [b for b in usable if b["link"] not in taken and _plausible(b, label, gap)]
        books = _pick(s, pool, label, author, gap) if pool else []
        taken.update(b["link"] for b in books)
        out.append({"seq": gap["seq"], "status": "found" if books else "missing",
                    "books": books})
    log.info("%r author sweep %r: %d of %d gap(s) filled", label, query,
             sum(1 for o in out if o["books"]), len(gaps))
    return out
