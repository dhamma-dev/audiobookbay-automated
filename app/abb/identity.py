"""Who is making this request?

Identity comes from the reverse proxy's forwarded auth headers (Authentik by
deployment; other forward-auth headers accepted as fallbacks). This is only
trustworthy because the app is reachable exclusively through the proxy — do
not build features that assume the header is unforgeable outside that setup.
"""

from __future__ import annotations

from flask import request


def current_user_label():
    """Best-effort identity for the download log: prefer Authentik's forwarded
    username, fall back through other common forward-auth headers, then the
    client IP."""
    h = request.headers
    return (h.get("X-authentik-username")
            or h.get("X-authentik-email")
            or h.get("Remote-User")                    # Authelia / generic forward-auth
            or h.get("X-Forwarded-Preferred-Username")
            or h.get("X-Forwarded-User")
            or request.remote_addr
            or "unknown")


def is_log_admin(user, config):
    """Admins (or everyone, if no allowlist is set) can see all log entries."""
    return (not config.log_admin_users) or (user in config.log_admin_users)
