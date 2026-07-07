"""Session, CSRF, and response-header hardening.

Threat model context: the app lives behind Authentik forward-auth on a
private network, so everything here is defense-in-depth, not the primary
gate. Three layers:

1. **Secret key** — from FLASK_SECRET_KEY, else generated once and persisted
   under the data dir, so sessions (route choice, Put.io OAuth tokens)
   survive restarts. v1 regenerated per boot.
2. **CSRF** — a per-session token. Form POSTs must carry it (hidden
   `csrf_token` field); fetch() calls send it as `X-CSRF-Token`. JSON POSTs
   without a token are allowed only when the Content-Type really is JSON —
   that content type cannot be produced cross-site without a CORS preflight
   this app never approves, and it keeps `/send` scriptable with curl.
3. **Headers** — CSP and friends on every response. The CSP allows only
   self-hosted scripts (icons are vendored; see static/js/icons.js), Google
   Fonts stylesheets, and https images (ABB covers are hotlinked unless
   COVER_PROXY is on).
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets

from flask import jsonify, request, session

log = logging.getLogger("abb.security")

CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data: https: http:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'"
)


def load_secret_key(config):
    """FLASK_SECRET_KEY when set; otherwise a key generated once and persisted
    (0600) in the data dir. Falls back to an ephemeral key — with a loud
    warning, since that logs everyone out on each restart — only when the data
    dir isn't writable."""
    if config.secret_key:
        return config.secret_key
    path = os.path.join(config.data_dir, "secret_key")
    try:
        with open(path, "r", encoding="ascii") as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        os.makedirs(config.data_dir, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(key)
        log.info("generated a persistent secret key at %s", path)
    except OSError as e:
        log.warning("could not persist a secret key (%s); sessions will reset "
                    "on every restart. Set FLASK_SECRET_KEY or mount %s.",
                    e, config.data_dir)
    return key


def get_csrf_token():
    """The session's CSRF token, minting one on first use."""
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(16)
        session["csrf_token"] = token
    return token


def init_security(app, config):
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.cookie_secure,
    )

    @app.context_processor
    def _inject_csrf():
        return {"csrf_token": get_csrf_token}

    @app.before_request
    def _check_csrf():
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return None
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
        expected = session.get("csrf_token")
        if sent and expected and hmac.compare_digest(sent, expected):
            return None
        # No (valid) token: JSON bodies get a pass — a real cross-site request
        # can't send this content type without a preflight we'd never approve,
        # and it keeps the JSON API usable from scripts.
        if request.mimetype == "application/json":
            return None
        log.warning("rejected %s %s: missing/invalid CSRF token", request.method, request.path)
        return jsonify({"message": "Invalid or missing CSRF token. Reload the page and try again."}), 403

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp
