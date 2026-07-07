"""In-app feature settings: SQLite-backed overrides for the feature-tier env
vars (config.FEATURE_SETTINGS), with most-recently-set-wins precedence.

Precedence per key, made deterministic instead of timestamp-guessing:

- Each saved override records the env value that was in effect at save time
  (its *snapshot*).
- If the current env value still equals the snapshot, the override was set
  more recently than the env → the **override wins** ("app").
- If the env value differs from the snapshot, the operator changed the
  deployment after the override was saved → **env wins** and the override is
  reported as "superseded" (kept, visibly inert, until re-saved or reset).
- No override → env ("env"), or the built-in default when the var is unset
  ("default").

The settings page shows this provenance per key, so there is never a mystery
about where an active value came from. Secrets are write-only: stored and
applied, never rendered back.
"""

from __future__ import annotations

from dataclasses import replace

from .config import FEATURE_SETTINGS, WANTED_ROUTE_CHOICES, Config, is_truthy


def coerce(env_key: str, raw: str | None, fallback):
    """Parse a stored override string into the Config field's type. Invalid
    values fall back to the base config's value rather than exploding a boot."""
    _field, kind = FEATURE_SETTINGS[env_key]
    raw = (raw or "").strip()
    if kind == "bool":
        return is_truthy(raw)
    if kind == "float":
        try:
            return float(raw)
        except ValueError:
            return fallback
    if kind == "choice":
        return raw if raw in WANTED_ROUTE_CHOICES else "default"
    if env_key == "ABS_URL":
        return raw.rstrip("/")
    if env_key == "GEMINI_API_KEY":
        return raw or None  # empty disables the feature, like an unset env var
    return raw


def effective_config(base: Config, store) -> tuple[Config, dict]:
    """Overlay stored overrides onto the env-built config. Returns the
    effective Config plus {env_key: provenance} for the settings page."""
    provenance = {}
    updates = {}
    rows = store.settings_all()
    for env_key, (field_name, _kind) in FEATURE_SETTINGS.items():
        env_raw = base.raw_env.get(env_key)
        row = rows.get(env_key)
        if row is not None and row.get("env_snapshot") == env_raw:
            updates[field_name] = coerce(env_key, row.get("value"),
                                         getattr(base, field_name))
            provenance[env_key] = "app"
        elif row is not None:
            provenance[env_key] = "superseded"
        else:
            provenance[env_key] = "env" if env_raw not in (None, "") else "default"
    return (replace(base, **updates) if updates else base), provenance
