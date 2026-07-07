"""Server-rendered pages: search, downloads, upgrades, wanted, log — plus the
optional cover proxy the search page uses when COVER_PROXY is on."""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from urllib.parse import urlparse

from flask import (Blueprint, Response, abort, render_template, request,
                   url_for)

from ..identity import current_user_label, is_log_admin
from ..scraper import USER_AGENT
from ..smart_sort import rank_payload
from ..wanted import wanted_queries
from . import svc

bp = Blueprint("pages", __name__)

# Tiny LRU for proxied covers so a page of results doesn't re-pull the same
# art through Tor on every render. Covers are small; 256 × ~50 KB ≈ 12 MB.
_cover_cache = OrderedDict()
_cover_lock = threading.Lock()
_COVER_CACHE_MAX = 256


@bp.route("/", methods=["GET", "POST"])
def search():
    s = svc()
    books = []
    # A search runs on the POSTed form, or on a shareable GET link (/?q=...) —
    # the Upgrade Radar uses the latter to deep-link straight into results.
    query = (request.form.get("query", "") if request.method == "POST"
             else request.args.get("q", "")).strip().lower()
    if query:
        # If this browser is set to Tor but Tor is still bootstrapping, don't
        # silently scrape over Direct — tell the client to wait (it polls and
        # re-enables) or switch to Direct.
        if s.outbound.route_mode() == "tor" and s.tor.status() != "ready":
            if request.method == "POST":
                return {"message": "Tor is still starting. Please wait a moment or switch to Direct.",
                        "tor_status": s.tor.status()}, 503
            query, books = "", []  # GET deep link while Tor boots: render unsearched
        else:
            books = s.scraper.search(query) or []  # None = mirror unreachable
            # Float preferred results to the top: matching-language first (when
            # PREFERRED_LANGUAGE is set), then M4B. Python's stable sort keeps
            # the mirror's original ordering within each group.
            books.sort(key=lambda b: (not s.config.language_matches(b), not b.get("is_m4b")))
    # Tag each result with a stable id so the client can ask Gemini to re-rank
    # them and then reorder the matching cards in place (see /api/rank).
    for i, book in enumerate(books):
        book["id"] = i
    s.library.annotate_matches(books)
    payload = rank_payload(books)  # built before covers are rewritten
    if s.config.cover_proxy:
        for book in books:
            proxied = _proxied_cover(book.get("cover"), s.config)
            if proxied:
                book["cover"] = proxied
    m4b_count = sum(1 for b in books if b.get("is_m4b"))
    owned_count = sum(1 for b in books if b.get("library_match"))
    return render_template("search.html", books=books, query=query,
                           searched=bool(query),
                           result_count=len(books), m4b_count=m4b_count,
                           owned_count=owned_count,
                           rank_payload=payload)


def _cover_host_allowed(url, config):
    """Only ABB-hosted art is ever proxied — this must not become an open
    proxy. Anything else keeps the ordinary hotlink."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    abb = config.abb_hostname.lower()
    return host == abb or host.endswith("." + abb)


def _proxied_cover(url, config):
    if url and url.startswith(("http://", "https://")) and _cover_host_allowed(url, config):
        return url_for("pages.cover", u=url)
    return None


@bp.route("/covers")
def cover():
    """Stream a result's cover through the server's route session, so browsers
    on Tor-shielded instances never touch the mirror directly. Enabled by
    COVER_PROXY; the allowlist keeps it from being an open proxy (SSRF)."""
    s = svc()
    if not s.config.cover_proxy:
        abort(404)
    url = request.args.get("u", "")
    if not _cover_host_allowed(url, s.config):
        abort(403)
    with _cover_lock:
        hit = _cover_cache.get(url)
        if hit:
            _cover_cache.move_to_end(url)
    if not hit:
        try:
            r = s.outbound.scrape_session().get(url, headers={"User-Agent": USER_AGENT},
                                                timeout=s.config.request_timeout)
            if r.status_code != 200 or not r.headers.get("Content-Type", "").startswith("image/"):
                abort(404)
            hit = (r.headers["Content-Type"], r.content)
        except Exception:
            abort(502)
        with _cover_lock:
            _cover_cache[url] = hit
            _cover_cache.move_to_end(url)
            while len(_cover_cache) > _COVER_CACHE_MAX:
                _cover_cache.popitem(last=False)
    content_type, body = hit
    return Response(body, mimetype=content_type,
                    headers={"Cache-Control": "public, max-age=86400"})


@bp.route("/status")
def status():
    s = svc()
    try:
        torrent_list = s.clients.list_torrents()
        return render_template("status.html", torrents=torrent_list)
    except Exception as e:
        return render_template("status.html", torrents=[], error=str(e))


@bp.route("/upgrades")
def upgrades():
    """Upgrade Radar: below-par owned copies (low effective bitrate,
    per-chapter MP3 rips) with a one-click ABB search for each. Everything is
    local arithmetic on the cached index. Worst copies first."""
    s = svc()
    if not s.library.enabled:
        return render_template("upgrades.html", enabled=False, flagged=[],
                               total=0, low_kbps=s.config.abs_low_kbps)
    return render_template("upgrades.html", enabled=True,
                           flagged=s.library.flagged_items(),
                           total=len(s.library.get_index()),
                           low_kbps=s.config.abs_low_kbps)


@bp.route("/wanted")
def wanted():
    """Hardcover wanted-list dashboard: every 'Want to Read' book with its
    pipeline status and, when found, the best ABB match ready to send."""
    s = svc()
    if not s.wanted.enabled:
        return render_template("wanted.html", enabled=False, active=[], owned=[],
                               skipped=[], counts={},
                               auto=s.config.wanted_auto_download, sync_error="")
    rows = sorted(s.store.wanted_rows(), key=lambda r: (r.get("title") or "").lower())
    # Three shelves: the ACTIVE pipeline (still doing or awaiting something),
    # books that are DONE (in the library — nothing left to do), and books
    # the user SKIPPED (out of the search rotation until re-allowed).
    order = {"found": 0, "wanted": 1, "unmatched": 2, "sent": 3}
    active = [r for r in rows if (r.get("status") or "wanted") in order]
    active.sort(key=lambda r: order[r.get("status") or "wanted"])
    owned = [r for r in rows if r.get("status") == "owned"]
    skipped = [r for r in rows if r.get("status") == "skipped"]
    for r in active:  # manual Search uses the same broad primary query as the worker
        r["search_q"] = wanted_queries(r.get("title") or "", r.get("author") or "")[0]
        try:  # stored candidates beyond the pick become the expandable tray
            alts = json.loads(r.get("candidates") or "[]")[1:]
        except ValueError:
            alts = []
        for a in alts:
            a["meta"] = " · ".join(x for x in (a.get("format"), a.get("bitrate"),
                                               a.get("size"), a.get("language"))
                                   if x and x not in ("Unknown", "?"))
        r["alts"] = alts
    counts = {}
    for r in rows:
        counts[r.get("status") or "wanted"] = counts.get(r.get("status") or "wanted", 0) + 1
    return render_template("wanted.html", enabled=True, active=active, owned=owned,
                           skipped=skipped, counts=counts,
                           auto=s.config.wanted_auto_download,
                           sync_error=s.wanted.sync_error,
                           route_label=s.wanted.route_label())


@bp.route("/log")
def download_log():
    """Audit log of who sent what. Admins (or everyone, if no allowlist is
    set) see all entries and can filter by user; anyone else sees only their
    own."""
    s = svc()
    user = current_user_label()
    admin = is_log_admin(user, s.config)
    # Non-admins are locked to their own rows regardless of any ?user= param.
    user_filter = (request.args.get("user") or None) if admin else user
    entries = s.store.fetch_download_log(user_filter=user_filter)
    return render_template("log.html", entries=entries, is_admin=admin,
                           user_filter=user_filter, log_enabled=s.store.enabled)
