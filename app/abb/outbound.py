"""Outbound HTTP sessions and per-request routing.

Keeps a plain (Direct) session and, when Tor is up, a Tor-proxied one, and
picks between them per request based on the browser's saved route choice
(session['route_mode']) or the USE_TOR default. Only AudiobookBay traffic ever
uses these sessions — Gemini, ABS, Hardcover, and the download client always
go direct via plain `requests` (that privacy boundary is deliberate).
"""

from __future__ import annotations

import logging

import requests
from flask import has_request_context, session as flask_session

log = logging.getLogger("abb.outbound")


class Outbound:
    def __init__(self, config, tor):
        self.config = config
        self.tor = tor
        self.direct_session = requests.Session()
        self._tor_session = None
        # Build the Tor session the moment Tor reports ready (also covers a
        # reused external Tor, which is ready synchronously in start()).
        tor.on_ready = self._build_tor_session

    def _make_tor_session(self):
        """socks5h keeps DNS resolution on the Tor side too, so the hostname
        never leaks."""
        s = requests.Session()
        proxy = f"socks5h://127.0.0.1:{self.config.tor_socks_port}"
        s.proxies = {"http": proxy, "https": proxy}
        return s

    def _build_tor_session(self):
        self._tor_session = self._make_tor_session()

    @property
    def tor_session(self):
        return self._tor_session

    def route_mode(self):
        """'tor' or 'direct' for the current request: the user's saved choice,
        or the USE_TOR default. Forced to 'direct' only when Tor is truly
        unavailable; while Tor is 'starting' the intended mode is kept (search
        is gated until it's ready, or the user can switch to Direct)."""
        if self.tor.status() == "unavailable":
            return "direct"
        # Background work (the wanted worker) has no request; use the default.
        mode = flask_session.get("route_mode") if has_request_context() else None
        if mode not in ("tor", "direct"):
            mode = "tor" if self.config.use_tor else "direct"
        return mode

    def scrape_session(self):
        """The requests session to use for AudiobookBay, per the active route."""
        if self.route_mode() == "tor" and self._tor_session is not None:
            return self._tor_session
        return self.direct_session

    def renew_tor_circuit(self):
        """New Tor exit + a fresh session so pooled connections don't keep the
        old circuit alive. Returns (ok, message)."""
        ok, message = self.tor.renew_circuit()
        if ok:
            self._build_tor_session()
        return ok, message
