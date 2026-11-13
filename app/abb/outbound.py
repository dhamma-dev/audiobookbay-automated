"""Outbound HTTP sessions and per-request routing.

Keeps a plain (Direct) session and, when Tor is up, a Tor-proxied one, and
picks between them per request based on the browser's saved route choice
(session['route_mode']) or the USE_TOR default. Only AudiobookBay traffic ever
uses these sessions — Gemini, ABS, Hardcover, and the download client always
go direct via plain `requests` (that privacy boundary is deliberate).

Routing FAILS CLOSED: when the route is Tor and no Tor circuit is usable
(still bootstrapping, failed to start, crashed), callers get TorUnavailable —
never a quiet Direct request, which would show the mirror this server's real
IP. Going Direct is always an explicit choice: a browser's toggle,
USE_TOR=false, or WANTED_ROUTE=direct.
"""

from __future__ import annotations

import logging

import requests
from flask import has_request_context, session as flask_session

log = logging.getLogger("abb.outbound")


class TorUnavailable(RuntimeError):
    """The route is Tor but no Tor circuit is usable right now."""

    def __init__(self, tor_status):
        self.tor_status = tor_status
        if tor_status == "starting":
            message = ("Tor is still starting — AudioBook Bay requests wait for it "
                       "instead of going direct. Try again in a moment, or switch to Direct.")
        else:
            message = ("Tor isn't available right now, so AudioBook Bay requests are paused "
                       "rather than sent directly (which would reveal this server's IP). "
                       "Switch to Direct to continue without Tor.")
        super().__init__(message)


class Outbound:
    def __init__(self, config, tor):
        self.config = config
        self.tor = tor
        self.direct_session = requests.Session()
        self.direct_session.abb_route = "direct"   # read back by route_of()
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
        s.abb_route = "tor"
        return s

    def _build_tor_session(self):
        self._tor_session = self._make_tor_session()

    @property
    def tor_session(self):
        return self._tor_session

    def route_mode(self):
        """The INTENDED route for the current request — 'tor' or 'direct': the
        browser's saved choice, else the USE_TOR default. Deliberately never
        downgraded when Tor is down; session_for() refuses instead."""
        # Background work (the wanted worker) has no request; use the default.
        mode = flask_session.get("route_mode") if has_request_context() else None
        if mode not in ("tor", "direct"):
            mode = "tor" if self.config.use_tor else "direct"
        return mode

    def tor_ready(self):
        """True when a Tor-routed request can go out right now."""
        return self.tor.status() == "ready" and self._tor_session is not None

    def session_for(self, mode):
        """The requests session for a route. Tor without a usable circuit
        raises TorUnavailable — the one thing this must never do is hand back
        the Direct session in its place."""
        if mode == "direct":
            return self.direct_session
        if self.tor_ready():
            return self._tor_session
        raise TorUnavailable(self.tor.status())

    def scrape_session(self):
        """The session for AudiobookBay, per the active route (may raise
        TorUnavailable)."""
        return self.session_for(self.route_mode())

    @staticmethod
    def route_of(sess):
        """'tor' or 'direct' for a session this object handed out — for the
        download log, which must record the route actually used."""
        return getattr(sess, "abb_route", "direct")

    def renew_tor_circuit(self):
        """New Tor exit + a fresh session so pooled connections don't keep the
        old circuit alive. Returns (ok, message)."""
        ok, message = self.tor.renew_circuit()
        if ok:
            self._build_tor_session()
        return ok, message
