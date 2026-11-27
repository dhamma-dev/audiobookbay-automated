"""Labs: the experiments page, and the library series sweep's review page.

Admin-only, gated like /settings. An experiment's own routes 404 until it is
switched on at /labs. Everything here is plain form POSTs (CSRF-checked) and
redirects; js/labs.js only adds filtering and the progress poll.
"""

from __future__ import annotations

from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for

from ..identity import current_user_label, is_log_admin
from ..library import AbsWriteError
from ..sweep import APPLY_MAX, EXPERIMENT
from . import svc

bp = Blueprint("labs", __name__)


def _admin():
    return is_log_admin(current_user_label(), svc().config)


@bp.route("/labs")
def labs_page():
    s = svc()
    if not _admin():
        return render_template("labs.html", allowed=False, cards=[]), 403
    return render_template("labs.html", allowed=True, cards=s.labs.cards(),
                           available=s.labs.available)


@bp.route("/labs/toggle", methods=["POST"])
def labs_toggle():
    s = svc()
    if not _admin():
        abort(403)
    key = request.form.get("key", "")
    try:
        s.labs.set(key, request.form.get("on") == "1", current_user_label())
    except KeyError:
        abort(404)
    if key == EXPERIMENT and request.form.get("on") != "1":
        s.sweep.cancel()        # switching it off stops a sweep in flight
    return redirect(url_for("labs.labs_page"))


# --- Library series sweep ---------------------------------------------------------
def _sweep_or_404():
    """The sweep's routes exist only for admins with the experiment on."""
    s = svc()
    if not _admin():
        abort(403)
    if not s.labs.enabled(EXPERIMENT):
        abort(404)
    return s


def _back(**args):
    return redirect(url_for("labs.series_page", **args))


@bp.route("/labs/series")
def series_page():
    s = _sweep_or_404()
    tab = "history" if request.args.get("tab") == "history" else "review"
    rows, counts = s.sweep.queue()
    return render_template(
        "labs_series.html", tab=tab, rows=rows, counts=counts,
        history=s.sweep.history() if tab == "history" else [],
        job=s.sweep.status(), abs_enabled=s.library.enabled,
        apply_max=APPLY_MAX, notice=request.args.get("notice"),
        error=request.args.get("error"))


@bp.route("/labs/series/run", methods=["POST"])
def series_run():
    s = _sweep_or_404()
    if not s.library.enabled:
        return _back(error="Audiobookshelf is not configured.")
    mode = "local" if request.form.get("mode") == "local" else "audible"
    s.sweep.start(mode, recheck=request.form.get("recheck") == "1")
    return _back()


@bp.route("/labs/series/cancel", methods=["POST"])
def series_cancel():
    _sweep_or_404().sweep.cancel()
    return _back()


@bp.route("/api/labs/series/status")
def series_status():
    return jsonify(_sweep_or_404().sweep.status())


@bp.route("/labs/series/apply", methods=["POST"])
def series_apply():
    s = _sweep_or_404()
    f = request.form
    # One row's own Skip / Not-a-series button submits the same form.
    for status in ("skipped", "standalone"):
        if f.get(status):
            s.sweep.dismiss(f.get(status), status)
            return _back()
    picks = [{"item": item_id, "series": f.get(f"series-{item_id}", ""),
              "seq": f.get(f"seq-{item_id}", "")} for item_id in f.getlist("item")]
    if not picks:
        return _back(error="Tick the books to label first.")
    written, already, errors = s.sweep.apply(picks, current_user_label())
    bits = []
    if written:
        bits.append(f"Labelled {written} book{'' if written == 1 else 's'} in Audiobookshelf.")
    if already:
        bits.append(f"{already} already had a number there.")
    if len(picks) > APPLY_MAX:
        bits.append(f"Only the first {APPLY_MAX} were applied.")
    return _back(notice=" ".join(bits) or None, error=errors[0] if errors else None)


@bp.route("/labs/series/restore", methods=["POST"])
def series_restore():
    """Bring back books marked 'not a series' or skipped."""
    s = _sweep_or_404()
    which = request.form.get("which")
    if which in ("standalone", "skipped"):
        for item_id, row in s.store.proposals().items():
            if row["status"] == which:
                s.sweep.dismiss(item_id, "pending")
    return _back()


@bp.route("/labs/series/undo", methods=["POST"])
def series_undo():
    s = _sweep_or_404()
    try:
        row = s.sweep.undo(int(request.form.get("id") or 0))
    except (AbsWriteError, ValueError) as e:
        return _back(tab="history", error=str(e))
    return _back(tab="history", notice=f"Undid the label on “{row.get('title') or 'the book'}”.")
