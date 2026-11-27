"""Library series sweep (a Labs experiment — see labs.py).

Looks at every owned Audiobookshelf book that has no series, or a series with
no number, and proposes the label. Nothing is written until an admin approves
rows on /labs/series; writes go through AbsLibrary.set_series (blanks only)
and are recorded so they can be undone.

Where a proposal comes from, most trusted first:
  1. Audible by the book's own ASIN — an exact lookup, no fuzzy matching.
  2. The book's own subtitle/title ("The Expanse, Book 2") — local, free.
  3. Audible by title + author — accepted only through the author-gated
     matcher (matching.best_match), like every other match in this app.

PRIVACY: this is the one place the app sends anything about the user's
library out — an ASIN, or a title + author, per unlabelled book, to Audible's
public catalogue, directly (not via Tor). That is why it lives behind an
off-by-default Labs switch, and why mode="local" (subtitles only) exists and
makes no request at all. Lookups are cached in the proposal row, so a re-run
doesn't ask again for RECHECK_DAYS.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests

from . import matching
from .library import AbsWriteError, _same_series, norm_seq

log = logging.getLogger("abb.sweep")

EXPERIMENT = "series_sweep"
AUDIBLE_URL = "https://api.audible.com/1.0/catalog/products"
AUDIBLE_GROUPS = "series,product_desc,contributors"
RECHECK_DAYS = 30          # how long a cached Audible answer is trusted
PACE_SECONDS = 1.0         # pause after each Audible request
APPLY_MAX = 300            # rows per Apply click

_NUM = r"(?P<seq>\d{1,4}(?:\.\d{1,2})?)"
_KW = r"(?:book|volume|vol\.?|part|#)"
# "The Completionist Chronicles Series, Book 2" / "Cradle, Book 3" / "Dune #2"
_NAME_THEN_NUM = re.compile(rf"^(?P<name>.+?)(?:\s+series)?\s*[,:–—-]?\s*{_KW}\s*{_NUM}\s*$", re.I)
# "Book 2 of The Expanse" / "Book 2 in the Expanse series"
_NUM_THEN_NAME = re.compile(rf"^{_KW}\s*{_NUM}\s+(?:of|in)\s+(?P<name>.+?)(?:\s+series)?\s*$", re.I)
_NUM_ONLY = re.compile(rf"^{_KW}\s*{_NUM}\s*$", re.I)
_SEQ_OK = re.compile(r"\d{1,4}(\.\d{1,2})?")
_ASIN = re.compile(r"[A-Z0-9]{10}")


def _clean_name(name):
    name = " ".join((name or "").strip(" ,:;–—-()[]").split())
    return re.sub(r"^(?:a|an)\s+", "", name, flags=re.I) if name else ""


def _parse_phrase(text):
    """(series name or '', seq) from one phrase, or None."""
    text = " ".join((text or "").split())
    if not text:
        return None
    m = _NUM_ONLY.match(text)
    if m:
        return "", norm_seq(m.group("seq"))
    for rx in (_NUM_THEN_NAME, _NAME_THEN_NUM):
        m = rx.match(text)
        if m and _clean_name(m.group("name")):
            return _clean_name(m.group("name")), norm_seq(m.group("seq"))
    return None


def parse_local(item):
    """Read a series label out of the book's own metadata — (name, seq) or
    None. `name` is '' when only the number is stated ("Book 2"). The
    subtitle first (Audible-matched books usually carry it there), then a
    trailing parenthetical or delimited tail of the title. A bare title is
    never parsed: "Catch-22" and "Part 2" style titles aren't labels."""
    hit = _parse_phrase(item.get("subtitle"))
    if hit:
        return hit
    title = item.get("title") or ""
    m = re.search(r"[(\[]([^()\[\]]+)[)\]]\s*$", title)
    if m:
        hit = _parse_phrase(m.group(1))
        if hit:
            return hit
    for sep in (": ", " - ", " – ", " — "):
        if sep in title:
            hit = _parse_phrase(title.split(sep, 1)[1])
            if hit:
                return hit
    return None


def needs_label(item):
    """No series at all, or filed in a series with no number."""
    series = item.get("series") or []
    return bool(item.get("id")) and (
        not series or any(q in (None, "") for _n, q in series))


def product_series(product):
    """[(name, seq)] from an Audible product, numbered entries only (Audible
    gives "1-3" for omnibus editions and "" for unnumbered — neither is a
    label we can write)."""
    out = []
    for s in product.get("series") or []:
        name = " ".join(str(s.get("title") or "").split())
        seq = norm_seq(s.get("sequence") or "")
        if name and _SEQ_OK.fullmatch(seq):
            out.append((name, seq))
    return out


def _slim(product, via, tier=""):
    """What we keep of an Audible answer (cached in the proposal row)."""
    return {"asin": product.get("asin") or "", "title": product.get("title") or "",
            "subtitle": product.get("subtitle") or "",
            "author": ", ".join(a.get("name", "") for a in product.get("authors") or []),
            "series": product_series(product), "via": via, "tier": tier}


def library_name(name, index):
    """The spelling the library already uses for this series, when it has one
    — so a sweep never creates "Cradle" beside an existing "The Cradle Series"."""
    seen = Counter(n for it in index for n, _q in it.get("series") or []
                   if n and _same_series(n, name))
    return seen.most_common(1)[0][0] if seen else name


def propose(item, audible, index):
    """Combine the local reading and the (cached) Audible answer into one
    proposal: {series, seq, confidence, source, note} or None. Pure."""
    local = parse_local(item)
    unnumbered = [n for n, q in item.get("series") or [] if n and q in (None, "")]
    if local and not local[0]:
        # "Book 2" alone only helps a book already filed in exactly one series.
        local = (unnumbered[0], local[1]) if len(unnumbered) == 1 else None

    remote = None
    if audible and audible.get("series"):
        options = [tuple(x) for x in audible["series"]]
        prefer = unnumbered + ([local[0]] if local else [])
        remote = next((o for o in options if any(_same_series(o[0], p) for p in prefer)),
                      options[0])

    note = ""
    if remote and audible["via"] == "id":
        series, seq, confidence, source = remote[0], remote[1], "high", "Audible ID"
        if local and local[1] != seq:
            note = f"its subtitle says #{local[1]}"
    elif remote and local:
        agree = local[1] == remote[1] and _same_series(local[0], remote[0])
        series, seq = remote
        confidence = "high" if agree else "low"
        source = "subtitle + Audible" if agree else "Audible search"
        if not agree:
            note = f"its subtitle says {local[0]} #{local[1]}"
    elif remote:
        series, seq = remote
        confidence = "medium" if audible.get("tier") == "strong" else "low"
        source = "Audible search"
    elif local:
        series, seq, confidence, source = local[0], local[1], "medium", "subtitle"
    else:
        return None
    # Keep the name the book is already filed under, else the library's
    # existing spelling of that series.
    filed = next((n for n in unnumbered if _same_series(n, series)), None)
    return {"series": filed or library_name(series, index), "seq": seq,
            "confidence": confidence, "source": source, "note": note}


class SeriesSweep:
    def __init__(self, services):
        self.s = services           # services.library is rebuilt on settings saves
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread = None
        self.job = {"running": False, "mode": "", "done": 0, "total": 0,
                    "error": "", "finished_at": ""}

    # --- Audible -------------------------------------------------------------------
    def _audible(self, path="", **params):
        r = requests.get(AUDIBLE_URL + path, timeout=20,
                         params=dict(params, response_groups=AUDIBLE_GROUPS))
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        return r.json()

    def lookup(self, item):
        """One book's Audible answer, slimmed — or {} when Audible has nothing
        we trust. At most two requests."""
        asin = (item.get("asin") or "").strip().upper()
        if _ASIN.fullmatch(asin):
            product = self._audible(f"/{asin}").get("product") or {}
            if product.get("title"):
                return _slim(product, "id")
        title = item.get("title") or ""
        author = (item.get("author") or "").split(",")[0].strip()
        if not title or not author:
            return {}        # a title alone is too loose to search on
        products = self._audible(title=title, author=author, num_results=10).get("products") or []
        # The author-gated matcher decides, with the roles swapped: our book
        # is the query, Audible's products are the shelf to match against.
        shelf = [{"title": p.get("title") or "", "subtitle": p.get("subtitle") or "",
                  "author": ", ".join(a.get("name", "") for a in p.get("authors") or []),
                  "series": [], "language": "", "_p": p}
                 for p in products if product_series(p)]
        tier, _score, hit, _reason = matching.best_match(
            {"raw": title, "title": title, "author": author, "language": ""}, shelf)
        if hit is None or tier == matching.NONE:
            return {}
        return _slim(hit["_p"], "search", "strong" if tier == matching.STRONG else "maybe")

    # --- the sweep -----------------------------------------------------------------
    def status(self):
        with self._lock:
            return dict(self.job)

    def start(self, mode, recheck=False):
        """Kick off a sweep in the background. False if one is running."""
        with self._lock:
            if self.job["running"]:
                return False
            self._cancel.clear()
            self.job = {"running": True, "mode": mode, "done": 0, "total": 0,
                        "error": "", "finished_at": ""}
        self._thread = threading.Thread(target=self._run, args=(mode, recheck),
                                        name="series-sweep", daemon=True)
        self._thread.start()
        return True

    def cancel(self):
        self._cancel.set()

    def _run(self, mode, recheck):
        error = ""
        try:
            self.sweep(mode, recheck)
        except Exception as e:
            log.warning("series sweep failed: %s", e)
            error = "The sweep stopped: " + (
                "couldn't reach Audible." if isinstance(e, requests.RequestException)
                else str(e))
        with self._lock:
            self.job.update(running=False, error=error,
                            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def sweep(self, mode="audible", recheck=False):
        """Refresh the review queue. Synchronous (the thread target; tests
        call it directly). mode: "audible" | "local"."""
        store, library = self.s.store, self.s.library
        index = library.get_index(max_age=0)
        todo = [it for it in index if needs_label(it)]
        rows = store.proposals()
        # Books that got labelled (or left the library) drop out of the queue;
        # applied/standalone rows stay as the record of what was decided.
        wanted_ids = {it["id"] for it in todo}
        for item_id, row in rows.items():
            if item_id not in wanted_ids and row["status"] not in ("applied", "standalone"):
                store.proposal_delete(item_id)
        with self._lock:
            self.job["total"] = len(todo)
        fresh_after = (datetime.now(timezone.utc) - timedelta(days=RECHECK_DAYS)
                       ).isoformat(timespec="seconds")
        for n, item in enumerate(todo, 1):
            if self._cancel.is_set():
                break
            row = rows.get(item["id"]) or {}
            if row.get("status") == "standalone":
                with self._lock:
                    self.job["done"] = n
                continue
            cached = _load(row.get("evidence"))
            audible, looked_at = cached.get("audible"), cached.get("audible_at") or ""
            if mode == "audible" and (recheck or looked_at < fresh_after):
                audible = self.lookup(item)
                looked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self._cancel.wait(PACE_SECONDS)
            proposal = propose(item, audible, index)
            # The write itself refuses anything but a blank; don't queue a
            # row it would turn down.
            if proposal and not library.label_proposal(item, proposal["series"], proposal["seq"]):
                proposal = None
            store.proposal_upsert({
                "item_id": item["id"], "title": item.get("title") or "",
                "author": item.get("author") or "",
                "current": library.match_info(item)["series"],
                "series": proposal["series"] if proposal else "",
                "seq": proposal["seq"] if proposal else "",
                "confidence": proposal["confidence"] if proposal else "",
                "source": proposal["source"] if proposal else "",
                "evidence": json.dumps({"audible": audible, "audible_at": looked_at,
                                        "note": proposal["note"] if proposal else ""}),
                # A new sweep brings skipped rows back; "none" = nothing found.
                "status": "pending" if proposal else "none",
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            with self._lock:
                self.job["done"] = n

    # --- review queue --------------------------------------------------------------
    def queue(self):
        """Rows for the review table, most confident first, plus counts."""
        library = self.s.library
        order = {"high": 0, "medium": 1, "low": 2}
        pending, counts = [], Counter()
        for row in self.s.store.proposals().values():
            counts[row["status"]] += 1
            if row["status"] != "pending":
                continue
            ev = _load(row.get("evidence"))
            a = ev.get("audible") or {}
            row = dict(row, note=ev.get("note") or "", audible=a,
                       audible_url=(f"https://www.audible.com/pd/{a['asin']}"
                                    if _ASIN.fullmatch(a.get("asin") or "") else ""),
                       abs_url=library.match_info({"id": row["item_id"]}).get("url", ""))
            pending.append(row)
        pending.sort(key=lambda r: (order.get(r["confidence"], 3),
                                    (r["series"] or "").lower(), _seq_key(r["seq"])))
        return pending, counts

    def apply(self, picks, user):
        """Write approved rows: picks = [{item, series, seq}] (series/seq as
        possibly edited on the page). Returns (written, already, errors)."""
        library, store = self.s.library, self.s.store
        written = already = 0
        errors = []
        for pick in picks[:APPLY_MAX]:
            try:
                outcome, item = library.set_series(pick["item"], pick["series"], pick["seq"],
                                                   user=user, source="sweep")
            except AbsWriteError as e:
                errors.append(str(e))
                if len(errors) >= 3:    # a dead ABS or a read-only token: stop
                    break
                continue
            store.proposal_status(pick["item"], "applied")
            written += outcome == "set"
            already += outcome == "already"
        return written, already, errors

    def dismiss(self, item_id, status):
        """skipped: hidden until the next sweep. standalone: never asked again."""
        if status in ("skipped", "standalone", "pending"):
            self.s.store.proposal_status(item_id, status)

    def history(self):
        rows = self.s.store.series_writes()
        for r in rows:
            r["before_text"] = _series_text(_load(r.get("before"), []))
            r["after_text"] = _series_text(_load(r.get("after"), []))
        return rows

    def undo(self, write_id):
        """Revert one recorded write (raises AbsWriteError when the book has
        changed since, or the write is unknown/already undone)."""
        store = self.s.store
        row = store.series_write(write_id)
        if not row or row.get("undone_at"):
            raise AbsWriteError("That write was already undone.")
        self.s.library.restore_series(row["item_id"], _load(row["before"], []),
                                      _load(row["after"], []))
        store.series_write_undone(write_id)
        # Back in the review queue, if the sweep proposed it.
        if (store.proposals().get(row["item_id"]) or {}).get("status") == "applied":
            store.proposal_status(row["item_id"], "pending")
        return row


def _load(text, default=None):
    try:
        value = json.loads(text) if text else None
    except ValueError:
        value = None
    return value if value is not None else ({} if default is None else default)


def _seq_key(seq):
    try:
        return float(seq)
    except (TypeError, ValueError):
        return 0.0


def _series_text(series):
    return ", ".join(f"{x['name']} #{x['sequence']}" if x.get("sequence") else x["name"]
                     for x in series if isinstance(x, dict) and x.get("name")) or "—"
