"""The in-app Settings page: feature-tier config editable without redeploying.

Gated like /log: LOG_ADMIN_USERS when set, otherwise everyone (family-trust
model — the Authentik proxy is the actual gate). Deployment plumbing
(DOWNLOAD_CLIENT, DL_*, Tor, LOG_*) is deliberately absent; see
config.FEATURE_SETTINGS for the editable set and abb/settings.py for the
most-recently-set-wins precedence. Secrets are write-only: the page shows
set/not-set and accepts a replacement, but never echoes a stored value.
"""

from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, url_for

from ..config import FEATURE_SETTINGS, WANTED_ROUTE_CHOICES
from ..identity import current_user_label, is_log_admin
from ..settings import effective_config
from . import svc

bp = Blueprint("admin", __name__)

# Page layout: (section title, section note, [(env key, label, help), ...]).
SECTIONS = [
    ("Smart sort", "Gemini re-ranking of search results. Only public result "
                   "metadata is ever sent — never links, covers, or your library.", [
        ("GEMINI_API_KEY", "Gemini API key",
         "Enables Smart sort and the wanted-list AI verdict. From Google AI Studio."),
        ("RANK_MODEL", "Gemini model", "Default: gemini-3.5-flash."),
        ("PREFERRED_LANGUAGE", "Preferred language",
         "Floats this language's results up and tells the AI to rank others lower. "
         "Blank = no preference."),
    ]),
    ("Audiobookshelf", "“In your library” badges and the Upgrade Radar. "
                       "Your library never leaves this server.", [
        ("ABS_URL", "Audiobookshelf URL", "e.g. https://audiobooks.example.com"),
        ("ABS_TOKEN", "Audiobookshelf API token", "Settings → Users → your user."),
        ("ABS_LIBRARY_ID", "Library ID", "Optional; defaults to the first book library."),
        ("ABS_LOW_KBPS", "Upgrade threshold (kbps)",
         "Owned copies at or below this effective bitrate are flagged. Default 63."),
    ]),
    ("Hardcover wanted list", "Background search of your “Want to Read” list.", [
        ("HARDCOVER_API_KEY", "Hardcover API key",
         "From hardcover.app account settings. Tokens expire every January 1st."),
        ("WANTED_AUTO_DOWNLOAD", "Auto-download found books",
         "Strictest gate only: confident match AND M4B."),
        ("WANTED_LLM", "AI verdict on found results",
         "One small Gemini call per found book, ever. Off = deterministic matching."),
        ("WANTED_ROUTE", "Background search route",
         "“default” follows the server's USE_TOR setting."),
    ]),
]

PROVENANCE_LABELS = {
    "app": "set in app",
    "env": "from environment",
    "default": "default",
    "superseded": "env changed — override inactive",
}


def _allowed():
    s = svc()
    return is_log_admin(current_user_label(), s.config)


def _view_model(s):
    cfg, provenance = effective_config(s.base_config, s.store)
    sections = []
    for title, note, fields in SECTIONS:
        items = []
        for env_key, label, help_text in fields:
            field_name, kind = FEATURE_SETTINGS[env_key]
            value = getattr(cfg, field_name)
            items.append({
                "key": env_key, "label": label, "help": help_text, "kind": kind,
                "provenance": provenance.get(env_key, "default"),
                "provenance_label": PROVENANCE_LABELS.get(provenance.get(env_key), ""),
                # Secrets are write-only: the template gets set/not-set, never the value.
                "is_set": bool(value) if kind == "secret" else None,
                "value": ("" if kind == "secret"
                          else ("%g" % value) if kind == "float"
                          else bool(value) if kind == "bool"
                          else (value or "")),
            })
        sections.append({"title": title, "note": note, "items": items})
    return sections


@bp.route("/settings")
def settings_page():
    s = svc()
    if not _allowed():
        return render_template("settings.html", allowed=False, sections=[],
                               store_enabled=True, route_choices=WANTED_ROUTE_CHOICES), 403
    return render_template("settings.html", allowed=True,
                           sections=_view_model(s),
                           store_enabled=s.store.enabled,
                           saved=request.args.get("saved"),
                           error=request.args.get("error"),
                           route_choices=WANTED_ROUTE_CHOICES)


@bp.route("/settings", methods=["POST"])
def settings_save():
    s = svc()
    if not _allowed():
        return render_template("settings.html", allowed=False, sections=[],
                               store_enabled=True, route_choices=WANTED_ROUTE_CHOICES), 403
    if not s.store.enabled:
        return redirect(url_for("admin.settings_page",
                                error="Settings need the data volume (LOG_DB_PATH) to persist."))

    user = current_user_label()
    cfg, _prov = effective_config(s.base_config, s.store)
    error = None
    for env_key, (field_name, kind) in FEATURE_SETTINGS.items():
        snapshot = s.base_config.raw_env.get(env_key)
        if request.form.get(f"reset_{env_key}"):
            s.store.settings_delete(env_key)
            continue
        current = getattr(cfg, field_name)
        if kind == "secret":
            if request.form.get(f"clear_{env_key}"):
                s.store.settings_set(env_key, "", snapshot, user)
            else:
                posted = (request.form.get(env_key) or "").strip()
                if posted:  # blank = keep whatever is set now
                    s.store.settings_set(env_key, posted, snapshot, user)
            continue
        # A field absent from the POST was not on the form (or, for
        # checkboxes, its presence sentinel wasn't) — never treat absence as
        # "set to empty", or a partial POST would write junk overrides.
        if kind == "bool":
            if f"present_{env_key}" not in request.form:
                continue
            posted = env_key in request.form
            if posted != bool(current):
                s.store.settings_set(env_key, "true" if posted else "false", snapshot, user)
        elif env_key not in request.form:
            continue
        elif kind == "float":
            posted = request.form[env_key].strip()
            try:
                if posted and float(posted) != float(current):
                    s.store.settings_set(env_key, posted, snapshot, user)
            except ValueError:
                error = f"{env_key} must be a number — kept the previous value."
        elif kind == "choice":
            posted = request.form[env_key].strip().lower()
            if posted in WANTED_ROUTE_CHOICES and posted != current:
                s.store.settings_set(env_key, posted, snapshot, user)
        else:  # str
            posted = request.form[env_key].strip()
            if posted != (current or ""):
                s.store.settings_set(env_key, posted, snapshot, user)

    s.reload_settings()  # changes take effect now — no restart
    return redirect(url_for("admin.settings_page", saved=1, error=error))
