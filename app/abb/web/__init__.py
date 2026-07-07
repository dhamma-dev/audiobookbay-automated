"""Blueprints. Each module pulls the service container off the current app —
routes hold no state of their own."""

from __future__ import annotations

from flask import current_app


def svc():
    """The Services container wired up by create_app()."""
    return current_app.extensions["abb"]
