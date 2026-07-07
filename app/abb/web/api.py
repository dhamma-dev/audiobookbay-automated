"""JSON endpoints the client JS polls or posts to."""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from .. import matching
from ..smart_sort import sanitize_results
from . import svc

log = logging.getLogger("abb.api")

bp = Blueprint("api", __name__)


@bp.route("/api/rank", methods=["POST"])
def api_rank():
    """Re-rank a set of already-loaded results with Gemini. The client posts
    the query and the slim per-result metadata it was given; we never
    re-scrape the mirror here."""
    s = svc()
    if not s.rank.enabled:
        return jsonify({"message": "Smart sort is not configured."}), 503

    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    incoming = data.get("results") or []
    if not query or not incoming:
        return jsonify({"message": "Missing query or results."}), 400

    # Re-sanitize on the server too: only ids and the allowed fields are ever
    # forwarded to Gemini, regardless of what the client sent.
    results = sanitize_results(incoming)
    if not results:
        return jsonify({"message": "No rankable results."}), 400

    try:
        ranking = s.rank.rank(query, results, want_ownership=s.library.enabled)
        if s.library.enabled:
            s.library.resolve_ownership(ranking, s.library.get_index(), results)
        return jsonify(ranking)
    except Exception as e:
        log.error("smart sort failed: %s", e)
        return jsonify({"message": f"Smart sort failed: {e}"}), 502


@bp.route("/api/ownership", methods=["POST"])
def api_ownership():
    """Re-check ownership of results already on the page against a freshly-ish
    ABS index, so a book that finished downloading into Audiobookshelf can
    flip to 'in your library' in place — no re-search, no LLM. The client
    sends only the identities of results it currently shows as un-owned."""
    s = svc()
    if not s.library.enabled:
        return jsonify({"ownership": []})
    data = request.get_json(silent=True) or {}
    items = data.get("items") or []
    index = s.library.get_index(max_age=120)  # at most one ABS fetch per ~2 min
    if not index:
        return jsonify({"ownership": []})
    owned_series = s.library.owned_series_index(index)
    ownership = []
    for it in items:
        rid = it.get("id")
        if rid is None:
            continue
        # Without a canonical author (page not smart-sorted), split it out of
        # the raw ABB title so the matcher can author-gate, mirroring the
        # initial pass.
        if not it.get("author") and not it.get("series"):
            title, author = matching.split_title_author(it.get("title", ""))
            it = {**it, "title": title, "author": author}
        if s.library.canonical_owned(it, index, owned_series) is not None:
            ownership.append({"id": rid, "status": "owned"})
    return jsonify({"ownership": ownership})


@bp.route("/api/status")
def api_status():
    try:
        return jsonify({"torrents": svc().clients.list_torrents()})
    except Exception as e:
        return jsonify({"message": f"Failed to fetch torrent status: {e}"}), 500


@bp.route("/api/connection")
def api_connection():
    """Lightweight poll target so the search page can wait for Tor to finish
    bootstrapping and enable itself the moment routing is usable."""
    s = svc()
    return jsonify({
        "tor_status": s.tor.status(),
        "route_mode": s.outbound.route_mode(),
        "tor_available": s.tor.available,
        "tor_renewable": s.tor.renewable,
    })


@bp.route("/healthz")
def healthz():
    """Liveness for Docker/monitoring. Cheap on purpose: no outbound calls."""
    s = svc()
    return jsonify({"status": "ok", "tor": s.tor.status(),
                    "client": s.config.download_client or None})
