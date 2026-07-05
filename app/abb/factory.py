"""Application factory: build the config, wire the services, register the
blueprints. This is the ONLY place anything is constructed or started —
importing modules never launches Tor, opens the database, or spawns threads.
"""

from __future__ import annotations

import logging

from flask import Flask, session

from .clients import ClientRegistry
from .config import CLIENT_LABELS, Config
from .identity import current_user_label, is_log_admin
from .library import AbsLibrary
from .outbound import Outbound
from .scraper import Scraper
from .security import init_security, load_secret_key
from .smart_sort import RankService
from .storage import Store
from .tor import TorManager
from .wanted import WantedService

log = logging.getLogger("abb.app")


class Services:
    """Everything the routes and the worker need, wired once. Blueprints get
    at it via current_app.extensions['abb']."""

    def __init__(self, config: Config):
        # base_config is the env-derived truth; config is the *effective* one
        # after in-app settings overlay (reload_settings). They start equal.
        self.base_config = config
        self.config = config
        self._started = False
        self.store = Store(config)
        self.tor = TorManager(config)
        self.outbound = Outbound(config, self.tor)
        self.scraper = Scraper(config, self.outbound)
        self.library = AbsLibrary(config)
        self.rank = RankService(config)
        self.clients = ClientRegistry(config)
        self.wanted = WantedService(config, self.store, self.scraper, self.library,
                                    self.rank, self.clients, self.outbound, self.tor)

    def reload_settings(self):
        """Recompute the effective config from env + stored overrides and
        rebuild the services those settings feed. Called at boot (after the
        store opens) and after every settings-page save, so changes apply
        without a restart. Deployment-tier services (tor/outbound/scraper/
        store/clients) bind env-only settings and are left alone."""
        from .settings import effective_config

        cfg, _prov = effective_config(self.base_config, self.store)
        self.config = cfg
        self.rank = RankService(cfg)
        self.library = AbsLibrary(cfg)
        old_wanted = self.wanted
        self.wanted = WantedService(cfg, self.store, self.scraper, self.library,
                                    self.rank, self.clients, self.outbound, self.tor)
        if self._started:
            old_wanted.stop()       # the retired worker exits at its next tick
            self.wanted.start()

    def start(self):
        """The side effects, in dependency order: database, settings overlay,
        Tor (background bootstrap — serving starts immediately), then the
        wanted worker."""
        self.store.init()
        self.reload_settings()      # not yet _started: rebuild only, no threads
        self.tor.start()
        self.wanted.start()
        self._started = True


def _setup_logging(level_name: str):
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname).1s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
        root.addHandler(handler)
    root.setLevel(getattr(logging, level_name, logging.INFO))


def create_app(config: Config | None = None, start: bool = True) -> Flask:
    """Build the app. `start=False` skips the side effects (no Tor process, no
    DB init, no worker thread) — that's the mode tests and tools use."""
    config = config or Config.from_env()
    _setup_logging(config.log_level)

    app = Flask(__name__)  # templates/ and static/ live inside the abb package
    app.secret_key = load_secret_key(config)
    init_security(app, config)

    services = Services(config)
    app.extensions["abb"] = services

    from .web import actions, admin, api, pages, putio
    app.register_blueprint(pages.bp)
    app.register_blueprint(actions.bp)
    app.register_blueprint(api.bp)
    app.register_blueprint(putio.bp)
    app.register_blueprint(admin.bp)

    @app.context_processor
    def inject_app_config():
        c = services.config
        client = c.download_client or ""
        is_putio = client == "putio"
        putio_authenticated = is_putio and bool(services.clients.putio_token())
        return {
            "nav_link_name": c.nav_link_name,
            "nav_link_url": c.nav_link_url,
            "download_client": client,
            "download_client_label": CLIENT_LABELS.get(client, "Download Client"),
            # Shown app-wide when DOWNLOAD_CLIENT is unset/unknown or missing env.
            "client_config_error": None if services.clients.ok else services.clients.config_error,
            # Prompt for auth only when put.io is selected but we have no token.
            "show_putio_banner": is_putio and not putio_authenticated,
            "putio_authenticated": putio_authenticated,
            # OAuth login is offered when an OAuth app is configured.
            "putio_oauth_available": is_putio and bool(c.putio_client_id and c.putio_client_secret),
            # The user logged in via the OAuth flow (vs. a static env token).
            "putio_session_login": is_putio and "putio_access_token" in session,
            "smart_sort_available": services.rank.enabled,
            "smart_prefetch": session.get("smart_prefetch", c.smart_prefetch_default),
            "abs_match_enabled": services.library.enabled,
            "log_enabled": services.store.enabled,
            "wanted_enabled": services.wanted.enabled,
            # Connection controls (Tor routing toggle + circuit renewal).
            "tor_available": services.tor.available,
            "tor_renewable": services.tor.renewable,
            "tor_status": services.tor.status(),
            "route_mode": services.outbound.route_mode(),
            # The Settings nav entry follows the same gate as the page itself.
            "is_settings_admin": is_log_admin(current_user_label(), c),
            "page_title_suffix": "AudiobookBay",
        }

    for line in config.report():
        log.info("%s", line)

    if start:
        services.start()

    return app
