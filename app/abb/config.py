"""Typed application config, read once from the environment.

Every v1 env var is honoured with the same name, default, and parsing quirks
(e.g. REQUEST_TIMEOUT="off", negative RANK_THINKING_BUDGET). New v2 knobs are
additive and optional. ``Config.from_env`` takes a mapping so tests can build
configs without touching the real environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

# Clients the registry supports; label is what the UI shows on buttons.
CLIENT_LABELS = {
    "qbittorrent": "qBittorrent",
    "transmission": "Transmission",
    "delugeweb": "Deluge",
    "putio": "Put.io",
}

# Env each client must have to be usable. put.io authenticates per request
# (OAuth or static token), so its readiness is the "Connect Put.io" banner's
# job rather than a hard config check here.
CLIENT_REQUIRED_ENV = {
    "qbittorrent": ("DL_HOST", "DL_PORT", "DL_USERNAME", "DL_PASSWORD"),
    "transmission": ("DL_HOST", "DL_PORT", "DL_USERNAME", "DL_PASSWORD"),
    "delugeweb": ("DL_URL", "DL_PASSWORD"),
    "putio": (),
}


def is_truthy(value: str | None) -> bool:
    return (value or "").lower() not in ("0", "false", "no", "off", "")


def _parse_timeout(raw: str | None) -> float | None:
    """"45" -> 45.0; "0"/"off"/"none" -> None (unbounded, the old behaviour)."""
    v = (raw or "45").strip().lower()
    return None if v in ("0", "off", "none") else float(v)


def _parse_thinking_budget(raw: str | None) -> int | None:
    """Default 0 (no hidden thinking — measured ~4-5x faster with no quality
    loss). Positive N allows N thinking tokens; negative -> None (model
    default)."""
    if raw in (None, ""):
        return 0
    v = int(raw)
    return None if v < 0 else v


@dataclass
class Config:
    # --- AudiobookBay + outbound ---
    abb_hostname: str = "audiobookbay.lu"
    request_timeout: float | None = 45.0

    # --- Tor ---
    use_tor: bool = True            # default route for new visitors
    tor_autostart: bool = True
    tor_socks_port: int = 9050
    tor_control_port: int = 9051
    tor_bootstrap_timeout: int = 90

    # --- Download client ---
    download_client: str | None = None
    dl_url: str | None = None
    dl_scheme: str = "http"
    dl_host: str | None = None
    dl_port: str | None = None
    dl_username: str | None = None
    dl_password: str | None = None
    dl_category: str = "Audiobookbay-Audiobooks"
    save_path_base: str | None = None

    # --- put.io ---
    putio_client_id: str | None = None
    putio_client_secret: str | None = None
    putio_redirect_uri: str | None = None
    putio_access_token: str | None = None
    putio_save_parent_id: str | None = None

    # --- Nav link ---
    nav_link_name: str | None = None
    nav_link_url: str | None = None

    # --- Smart sort (Gemini) ---
    gemini_api_key: str | None = None
    rank_model: str = "gemini-3.5-flash"
    smart_prefetch_default: str = "off"     # "on" | "off"
    rank_thinking_budget: int | None = 0
    rank_cache_ttl: int = 900
    preferred_language: str = ""

    # --- Download log ---
    log_db_path: str = "/data/downloads.db"
    log_admin_users: frozenset[str] = field(default_factory=frozenset)

    # --- Audiobookshelf matching ---
    abs_url: str = ""
    abs_token: str = ""
    abs_library_id: str = ""
    abs_cache_ttl: int = 900
    abs_low_kbps: float = 63.0

    # --- Hardcover wanted list ---
    hardcover_api_key: str = ""
    hardcover_sync_ttl: int = 21600
    wanted_research_ttl: int = 86400
    wanted_retry_ttl: int = 1800
    wanted_auto_download: bool = False
    wanted_route: str = "default"           # "default" | "tor" | "direct"
    wanted_llm: bool = True

    # --- v2 additions ---
    secret_key: str | None = None           # FLASK_SECRET_KEY; else persisted
    log_level: str = "INFO"
    cookie_secure: bool = False             # set true when served over HTTPS
    cover_proxy: bool = False               # proxy covers through the route session

    @classmethod
    def from_env(cls, env=None) -> "Config":
        env = os.environ if env is None else env
        g = env.get

        dl_url = g("DL_URL")
        dl_scheme, dl_host, dl_port = g("DL_SCHEME", "http"), g("DL_HOST"), g("DL_PORT")
        if dl_url:
            parsed = urlparse(dl_url)
            dl_scheme = parsed.scheme or dl_scheme
            dl_host = parsed.hostname
            dl_port = str(parsed.port) if parsed.port else None
        elif dl_host and dl_port:
            # Deluge wants a URL; synthesize one when only host/port are given.
            dl_url = f"{dl_scheme}://{dl_host}:{dl_port}"

        return cls(
            abb_hostname=g("ABB_HOSTNAME", "audiobookbay.lu"),
            request_timeout=_parse_timeout(g("REQUEST_TIMEOUT")),
            use_tor=is_truthy(g("USE_TOR", "true")),
            tor_autostart=is_truthy(g("TOR_AUTOSTART", "true")),
            tor_socks_port=int(g("TOR_SOCKS_PORT", "9050")),
            tor_control_port=int(g("TOR_CONTROL_PORT", "9051")),
            tor_bootstrap_timeout=int(g("TOR_BOOTSTRAP_TIMEOUT", "90")),
            download_client=g("DOWNLOAD_CLIENT") or None,
            dl_url=dl_url,
            dl_scheme=dl_scheme,
            dl_host=dl_host,
            dl_port=dl_port,
            dl_username=g("DL_USERNAME"),
            dl_password=g("DL_PASSWORD"),
            dl_category=g("DL_CATEGORY", "Audiobookbay-Audiobooks"),
            save_path_base=g("SAVE_PATH_BASE"),
            putio_client_id=g("PUTIO_CLIENT_ID"),
            putio_client_secret=g("PUTIO_CLIENT_SECRET"),
            putio_redirect_uri=g("PUTIO_REDIRECT_URI"),
            putio_access_token=g("PUTIO_ACCESS_TOKEN"),
            putio_save_parent_id=g("PUTIO_SAVE_PARENT_ID"),
            nav_link_name=g("NAV_LINK_NAME"),
            nav_link_url=g("NAV_LINK_URL"),
            gemini_api_key=g("GEMINI_API_KEY") or None,
            rank_model=g("RANK_MODEL", "gemini-3.5-flash"),
            smart_prefetch_default="on" if is_truthy(g("SMART_PREFETCH_DEFAULT", "off")) else "off",
            rank_thinking_budget=_parse_thinking_budget(g("RANK_THINKING_BUDGET")),
            rank_cache_ttl=int(g("RANK_CACHE_TTL", "900")),
            preferred_language=(g("PREFERRED_LANGUAGE") or "").strip(),
            log_db_path=g("LOG_DB_PATH", "/data/downloads.db"),
            log_admin_users=frozenset(
                u.strip() for u in g("LOG_ADMIN_USERS", "").split(",") if u.strip()
            ),
            abs_url=(g("ABS_URL") or "").strip().rstrip("/"),
            abs_token=(g("ABS_TOKEN") or "").strip(),
            abs_library_id=(g("ABS_LIBRARY_ID") or "").strip(),
            abs_cache_ttl=int(g("ABS_CACHE_TTL", "900")),
            abs_low_kbps=float(g("ABS_LOW_KBPS", "63")),
            hardcover_api_key=(g("HARDCOVER_API_KEY") or "").strip(),
            hardcover_sync_ttl=int(g("HARDCOVER_SYNC_TTL", "21600")),
            wanted_research_ttl=int(g("WANTED_RESEARCH_TTL", "86400")),
            wanted_retry_ttl=int(g("WANTED_RETRY_TTL", "1800")),
            wanted_auto_download=is_truthy(g("WANTED_AUTO_DOWNLOAD", "false")),
            wanted_route=(g("WANTED_ROUTE") or "default").strip().lower(),
            wanted_llm=is_truthy(g("WANTED_LLM", "true")),
            secret_key=g("FLASK_SECRET_KEY") or None,
            log_level=(g("LOG_LEVEL") or "INFO").upper(),
            cookie_secure=is_truthy(g("COOKIE_SECURE", "false")),
            cover_proxy=is_truthy(g("COVER_PROXY", "false")),
        )

    # --- Derived flags -------------------------------------------------------
    @property
    def log_enabled(self) -> bool:
        return bool(self.log_db_path)

    @property
    def abs_enabled(self) -> bool:
        return bool(self.abs_url and self.abs_token)

    @property
    def wanted_enabled(self) -> bool:
        return bool(self.hardcover_api_key)

    @property
    def smart_sort_enabled(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def data_dir(self) -> str:
        """Where persistent state lives (the log DB's directory, or ./data)."""
        return os.path.dirname(self.log_db_path) or "data" if self.log_db_path else "data"

    def language_matches(self, book) -> bool:
        """True when a result's language looks like the preferred one (or when
        no preference is configured). Substring + case-insensitive, since the
        mirror's language field is free text ("English", "english", "Eng")."""
        if not self.preferred_language:
            return True
        lang = (book.get("language") or "").strip().lower()
        if not lang:
            return True  # unknown language -> don't penalize; let other signals decide
        pref = self.preferred_language.lower()
        return pref in lang or lang in pref

    def validate_client(self) -> tuple[bool, str]:
        """DOWNLOAD_CLIENT is required, must name a supported client, and that
        client must have the env it needs — a misconfigured deploy fails loudly
        (startup log + in-app banner) instead of silently doing nothing."""
        choices = ", ".join(CLIENT_LABELS)
        if not self.download_client:
            return False, f"No download client configured. Set DOWNLOAD_CLIENT to one of: {choices}."
        if self.download_client not in CLIENT_LABELS:
            return False, f"Unknown DOWNLOAD_CLIENT '{self.download_client}'. Choose one of: {choices}."
        values = {"DL_HOST": self.dl_host, "DL_PORT": self.dl_port, "DL_USERNAME": self.dl_username,
                  "DL_PASSWORD": self.dl_password, "DL_URL": self.dl_url}
        missing = [n for n in CLIENT_REQUIRED_ENV[self.download_client] if not values.get(n)]
        if missing:
            return False, (f"{CLIENT_LABELS[self.download_client]} is selected but these required "
                           f"settings are missing: {', '.join(missing)}.")
        return True, ""

    def report(self) -> list[str]:
        """Startup summary, one line per subsystem, secrets masked."""
        def onoff(x):
            return "set" if x else "not set"

        ok, err = self.validate_client()
        lines = [
            f"ABB host: {self.abb_hostname} (timeout {self.request_timeout or 'unbounded'}s)",
            f"Default route: {'Tor' if self.use_tor else 'Direct'}"
            + (f", SOCKS 127.0.0.1:{self.tor_socks_port}" if self.tor_autostart else " (autostart off)"),
            f"Download client: {self.download_client or '(not set)'}"
            + ("" if ok else f"  [CONFIG ERROR] {err}"),
        ]
        if self.download_client in ("qbittorrent", "transmission", "delugeweb"):
            lines.append(
                f"  target: {self.dl_url or f'{self.dl_scheme}://{self.dl_host}:{self.dl_port}'}"
                f", user {self.dl_username or '(none)'}, password {onoff(self.dl_password)}"
                f", category {self.dl_category}, save path {self.save_path_base or '(client default)'}")
        if self.download_client == "putio":
            lines.append(
                f"  put.io: client id {onoff(self.putio_client_id)}, secret {onoff(self.putio_client_secret)}"
                f", static token {onoff(self.putio_access_token)}, folder {self.putio_save_parent_id or '(root)'}")
        lines += [
            "Smart sort: " + (f"enabled ({self.rank_model}, thinking={self.rank_thinking_budget})"
                              if self.smart_sort_enabled else "disabled (no GEMINI_API_KEY)"),
            "ABS matching: " + (f"enabled ({self.abs_url})" if self.abs_enabled else "disabled"),
            "Download log: " + (self.log_db_path if self.log_enabled else "disabled (LOG_DB_PATH empty)")
            + (f" (admins: {', '.join(sorted(self.log_admin_users))})" if self.log_admin_users else ""),
            "Wanted list: " + ("enabled" + (", auto-download ON (M4B-only)" if self.wanted_auto_download
                                            else ", dashboard only")
                               if self.wanted_enabled else "disabled (no HARDCOVER_API_KEY)"),
        ]
        if self.preferred_language:
            lines.append(f"Preferred language: {self.preferred_language}")
        if self.cover_proxy:
            lines.append("Cover proxy: on (covers stream through the route session)")
        return lines
