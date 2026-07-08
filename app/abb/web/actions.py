"""State-changing endpoints: sending magnets, per-browser settings, Tor
renewal, and the wanted-list actions."""

from __future__ import annotations

import uuid
from urllib.parse import urlparse

from flask import Blueprint, jsonify, redirect, request, session, url_for

from ..clients import PutioNotConnected
from ..identity import current_user_label
from ..scraper import infohash_from_magnet
from . import svc

bp = Blueprint("actions", __name__)


def _is_abb_link(url, config):
    """True only for links on the configured AudiobookBay host. The /send
    endpoints take a URL from the client; without this check any authenticated
    user could make the server fetch arbitrary URLs (SSRF) via the scrape
    session — which, in Direct mode, originates from the server's real IP."""
    try:
        return (urlparse(url).hostname or "").lower() == config.abb_hostname.lower()
    except ValueError:
        return False


@bp.route("/send", methods=["POST"])
def send():
    s = svc()
    data = request.get_json(silent=True) or {}
    details_url = data.get("link")
    title = data.get("title")
    user = current_user_label()
    route = s.outbound.route_mode()
    if not details_url or not title:
        return jsonify({"message": "Invalid request"}), 400
    if not _is_abb_link(details_url, s.config):
        s.store.record_download(user, title, details_url, None, "error",
                                "Rejected: not an AudiobookBay link", route=route)
        return jsonify({"message": "Only AudiobookBay links can be sent."}), 400
    if not s.clients.ok:
        return jsonify({"message": s.clients.config_error}), 503

    try:
        magnet_link = s.scraper.extract_magnet_link(details_url)
        if not magnet_link:
            s.store.record_download(user, title, details_url, None, "error",
                                    "Failed to extract magnet link", route=route)
            return jsonify({"message": "Failed to extract magnet link"}), 500

        s.clients.add(magnet_link, title)
        s.store.record_download(user, title, details_url,
                                infohash_from_magnet(magnet_link), "ok", route=route)
        s.wanted.mark_sent_by_link(details_url)
        return jsonify({"message": "Download added successfully! This may take some time, "
                                   "the download will show in Audiobookshelf when completed."})
    except PutioNotConnected as e:
        s.store.record_download(user, title, details_url, None, "error", str(e), route=route)
        return jsonify({"message": str(e)}), 401
    except Exception as e:
        s.store.record_download(user, title, details_url, None, "error", str(e), route=route)
        return jsonify({"message": str(e)}), 500


@bp.route("/send/batch", methods=["POST"])
def send_batch():
    """Send a whole set of magnets at once (series "send selected"). Each item
    is processed exactly like /send, but they share one batch_id in the log so
    the operator can see they were added together, and the response reports
    per-item so the UI can show partial success (a dead torrent shouldn't sink
    the rest)."""
    s = svc()
    data = request.get_json(silent=True) or {}
    items = data.get("items") or []                       # [{link, title}, ...]
    batch_label = (data.get("batch_label") or "").strip() or None
    user = current_user_label()
    route = s.outbound.route_mode()
    if not items:
        return jsonify({"message": "No items to send"}), 400
    if not s.clients.ok:
        return jsonify({"message": s.clients.config_error}), 503

    batch_id = uuid.uuid4().hex[:12]
    results, sent = [], 0
    for item in items:
        link = (item.get("link") or "").strip()
        title = (item.get("title") or "").strip()
        if not link or not title:
            results.append({"link": link, "title": title, "ok": False,
                            "error": "Missing link or title"})
            continue
        if not _is_abb_link(link, s.config):
            s.store.record_download(user, title, link, None, "error",
                                    "Rejected: not an AudiobookBay link",
                                    batch_id, batch_label, route=route)
            results.append({"link": link, "title": title, "ok": False,
                            "error": "Not an AudiobookBay link"})
            continue
        try:
            magnet_link = s.scraper.extract_magnet_link(link)
            if not magnet_link:
                s.store.record_download(user, title, link, None, "error",
                                        "Failed to extract magnet link",
                                        batch_id, batch_label, route=route)
                results.append({"link": link, "title": title, "ok": False,
                                "error": "No magnet found"})
                continue
            s.clients.add(magnet_link, title)
            s.store.record_download(user, title, link, infohash_from_magnet(magnet_link),
                                    "ok", "", batch_id, batch_label, route=route)
            s.wanted.mark_sent_by_link(link)
            results.append({"link": link, "title": title, "ok": True})
            sent += 1
        except Exception as e:
            s.store.record_download(user, title, link, None, "error", str(e),
                                    batch_id, batch_label, route=route)
            results.append({"link": link, "title": title, "ok": False, "error": str(e)})

    return jsonify({"batch_id": batch_id, "sent": sent, "total": len(items),
                    "results": results})


@bp.route("/settings/route", methods=["POST"])
def set_route():
    """Persist this browser's AudiobookBay routing choice (tor|direct)."""
    s = svc()
    data = request.get_json(silent=True) or {}
    mode = data.get("mode")
    if mode not in ("tor", "direct"):
        return jsonify({"message": "Invalid route mode."}), 400
    if mode == "tor" and s.tor.status() == "unavailable":
        return jsonify({"message": "Tor is not available on this server."}), 409
    session["route_mode"] = mode
    session.permanent = True  # remember the choice across browser restarts
    return jsonify({"mode": mode})


@bp.route("/settings/prefetch", methods=["POST"])
def set_prefetch():
    """Persist this browser's smart-sort prefetch preference (on|off)."""
    data = request.get_json(silent=True) or {}
    mode = data.get("mode")
    if mode not in ("on", "off"):
        return jsonify({"message": "Invalid mode."}), 400
    session["smart_prefetch"] = mode
    session.permanent = True
    return jsonify({"mode": mode})


@bp.route("/tor/renew", methods=["POST"])
def tor_renew():
    """Request a fresh Tor circuit (new exit IP) for everyone on this instance."""
    ok, message = svc().outbound.renew_tor_circuit()
    return jsonify({"message": message}), (200 if ok else 409)


@bp.route("/wanted/sync", methods=["POST"])
def wanted_sync():
    """Refresh the list from Hardcover now and make every open row due for a
    re-search; the background worker drains them a few per minute."""
    s = svc()
    if not s.wanted.enabled:
        return jsonify({"message": "Hardcover is not configured."}), 503
    try:
        s.wanted.sync_list()
    except Exception as e:
        return jsonify({"message": f"Hardcover sync failed: {e}"}), 502
    s.wanted.requeue_unresolved()
    s.wanted.sweep_owned_now()  # "Sync now" also answers "did my sends land?"
    return redirect(url_for("pages.wanted"))


@bp.route("/wanted/add", methods=["POST"])
def wanted_add():
    """Quick-add a book by hand: library check first (instant "you already
    have this"), then an immediate search over the requester's route; a find
    auto-downloads on their behalf, otherwise the row joins the normal queue.
    Fire and forget."""
    s = svc()
    if not s.wanted.enabled:
        return jsonify({"message": "The wanted list is not configured."}), 503
    title = (request.form.get("title") or "").strip()[:300]
    author = (request.form.get("author") or "").strip()[:200]
    if not title:
        return redirect(url_for("pages.wanted", added="invalid"))
    user = current_user_label()
    outcome, hc_id = s.wanted.add_manual(title, author, user)
    if outcome != "added":
        return redirect(url_for("pages.wanted", added=outcome, t=title))
    row = next((r for r in s.store.wanted_rows() if r["hc_id"] == hc_id), None)
    status = s.wanted.search_and_autodownload(row)
    if status == "unreachable":
        return redirect(url_for("pages.wanted", added="unreachable", t=title))
    # Report where the row actually ended up (auto-send may have flipped
    # found -> sent already).
    fresh = next((r for r in s.store.wanted_rows() if r["hc_id"] == hc_id), None)
    return redirect(url_for("pages.wanted",
                            added=(fresh or {}).get("status") or status, t=title))


@bp.route("/wanted/remove/<int(signed=True):hc_id>", methods=["POST"])
def wanted_remove(hc_id):
    """Delete a manually-added book (Hardcover rows are removed on Hardcover)."""
    s = svc()
    if not s.wanted.enabled:
        return jsonify({"message": "The wanted list is not configured."}), 503
    ok, message = s.wanted.remove_manual(hc_id)
    if not ok:
        return jsonify({"message": message}), 409
    return redirect(url_for("pages.wanted"))


@bp.route("/wanted/skip/<int(signed=True):hc_id>", methods=["POST"])
def wanted_skip(hc_id):
    """Take an unfound book out of the search rotation until re-allowed."""
    s = svc()
    if not s.wanted.enabled:
        return jsonify({"message": "Hardcover is not configured."}), 503
    ok, message = s.wanted.skip(hc_id)
    if not ok:
        return jsonify({"message": message}), 409
    return redirect(url_for("pages.wanted"))


@bp.route("/wanted/unskip/<int(signed=True):hc_id>", methods=["POST"])
def wanted_unskip(hc_id):
    """Put a skipped book back in the queue (due immediately)."""
    s = svc()
    if not s.wanted.enabled:
        return jsonify({"message": "Hardcover is not configured."}), 503
    ok, message = s.wanted.unskip(hc_id)
    if not ok:
        return jsonify({"message": message}), 409
    return redirect(url_for("pages.wanted"))


@bp.route("/wanted/research/<int(signed=True):hc_id>", methods=["POST"])
def wanted_research(hc_id):
    """Re-search one wanted book right now (synchronous — it's one scrape,
    over the requesting browser's route)."""
    s = svc()
    if not s.wanted.enabled:
        return jsonify({"message": "Hardcover is not configured."}), 503
    row = next((r for r in s.store.wanted_rows() if r["hc_id"] == hc_id), None)
    if not row:
        return jsonify({"message": "Unknown wanted book."}), 404
    # Same path as the worker: a find here auto-downloads too (when enabled).
    s.wanted.search_and_autodownload(row)
    return redirect(url_for("pages.wanted"))
