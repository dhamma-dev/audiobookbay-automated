"""put.io OAuth: the in-app "Log in with Put.io" flow. The resulting per-user
token lives in the Flask session (see ClientRegistry.putio_token — a static
PUTIO_ACCESS_TOKEN works without any of this)."""

from __future__ import annotations

import logging
import secrets
from urllib.parse import urlencode

import requests
from flask import Blueprint, jsonify, redirect, request, session, url_for

from . import svc

log = logging.getLogger("abb.putio")

bp = Blueprint("putio", __name__)


@bp.route("/putio/auth")
def putio_auth():
    s = svc()
    if s.config.download_client != "putio":
        return jsonify({"message": "put.io is not configured as the download client"}), 400
    if not s.config.putio_client_id:
        return jsonify({"message": "put.io client ID not configured"}), 400

    # Build the redirect URI from the current request so it works no matter
    # what host/port the app is reached on, and stash it for the callback.
    dynamic_redirect_uri = f"{request.host_url.rstrip('/')}/putio/callback"
    session["dynamic_redirect_uri"] = dynamic_redirect_uri
    # OAuth CSRF guard: the callback must echo this state back.
    state = secrets.token_urlsafe(16)
    session["putio_oauth_state"] = state

    auth_url = "https://api.put.io/v2/oauth2/authenticate?" + urlencode({
        "client_id": s.config.putio_client_id,
        "response_type": "code",
        "redirect_uri": dynamic_redirect_uri,
        "state": state,
    })
    log.info("starting OAuth with redirect URI %s", dynamic_redirect_uri)
    return redirect(auth_url)


@bp.route("/putio/callback")
def putio_callback():
    s = svc()
    if s.config.download_client != "putio":
        return jsonify({"message": "put.io is not configured as the download client"}), 400

    expected_state = session.pop("putio_oauth_state", None)
    if not expected_state or request.args.get("state") != expected_state:
        return jsonify({"message": "OAuth state mismatch — please start the login again."}), 400

    code = request.args.get("code")
    if not code:
        return jsonify({"message": "Authorization code not received"}), 400

    # Reuse the redirect URI from /putio/auth; fall back to the configured one.
    dynamic_redirect_uri = session.get("dynamic_redirect_uri") or s.config.putio_redirect_uri

    response = requests.post("https://api.put.io/v2/oauth2/access_token", data={
        "client_id": s.config.putio_client_id,
        "client_secret": s.config.putio_client_secret,
        "grant_type": "authorization_code",
        "redirect_uri": dynamic_redirect_uri,
        "code": code,
    }, timeout=s.config.request_timeout)
    if response.status_code != 200:
        return jsonify({"message": f"Failed to get access token: {response.text}"}), 400

    session["putio_access_token"] = response.json().get("access_token")
    return redirect(url_for("pages.search"))


@bp.route("/putio/logout", methods=["POST"])
def putio_logout():
    # POST (with the CSRF token) — logging someone out must not be triggerable
    # by an <img> tag. The nav renders this as a small form.
    session.pop("putio_access_token", None)
    session.pop("dynamic_redirect_uri", None)
    return redirect(url_for("pages.search"))
