"""The in-app settings layer: most-recently-set-wins precedence, the admin
page, write-only secrets, and live service reload."""

from abb import create_app
from abb.config import Config
from abb.settings import coerce, effective_config
from abb.storage import Store
from tests.conftest import make_config


def store_at(tmp_path):
    store = Store(make_config(log_db_path=str(tmp_path / "s.db")))
    store.init()
    return store


# --- precedence --------------------------------------------------------------
def test_env_wins_without_override(tmp_path):
    base = Config.from_env({"GEMINI_API_KEY": "from-env", "LOG_DB_PATH": ""})
    cfg, prov = effective_config(base, store_at(tmp_path))
    assert cfg.gemini_api_key == "from-env"
    assert prov["GEMINI_API_KEY"] == "env"
    assert prov["ABS_URL"] == "default"          # unset everywhere


def test_override_wins_while_env_unchanged(tmp_path):
    store = store_at(tmp_path)
    base = Config.from_env({"GEMINI_API_KEY": "from-env", "LOG_DB_PATH": ""})
    # Saved later than the env value (snapshot matches current env).
    store.settings_set("GEMINI_API_KEY", "from-app", "from-env", "admin")
    cfg, prov = effective_config(base, store)
    assert cfg.gemini_api_key == "from-app"
    assert prov["GEMINI_API_KEY"] == "app"


def test_env_change_supersedes_override(tmp_path):
    store = store_at(tmp_path)
    store.settings_set("GEMINI_API_KEY", "from-app", "old-env-value", "admin")
    # Operator redeployed with a NEW env value since the override was saved.
    base = Config.from_env({"GEMINI_API_KEY": "new-env-value", "LOG_DB_PATH": ""})
    cfg, prov = effective_config(base, store)
    assert cfg.gemini_api_key == "new-env-value"     # most recent set wins
    assert prov["GEMINI_API_KEY"] == "superseded"


def test_override_can_disable_a_feature(tmp_path):
    store = store_at(tmp_path)
    base = Config.from_env({"GEMINI_API_KEY": "from-env", "LOG_DB_PATH": ""})
    store.settings_set("GEMINI_API_KEY", "", "from-env", "admin")  # cleared in app
    cfg, _ = effective_config(base, store)
    assert cfg.gemini_api_key is None and not cfg.smart_sort_enabled


def test_coerce_kinds():
    assert coerce("WANTED_LLM", "true", False) is True
    assert coerce("WANTED_LLM", "off", True) is False
    assert coerce("ABS_LOW_KBPS", "48", 63.0) == 48.0
    assert coerce("ABS_LOW_KBPS", "not-a-number", 63.0) == 63.0  # invalid -> fallback
    assert coerce("WANTED_ROUTE", "tor", "default") == "tor"
    assert coerce("WANTED_ROUTE", "carrier-pigeon", "default") == "default"
    assert coerce("ABS_URL", "http://abs.local/", "") == "http://abs.local"


# --- the page + live reload ---------------------------------------------------
def settings_app(tmp_path, **overrides):
    cfg = make_config(log_db_path=str(tmp_path / "app.db"), **overrides)
    app = create_app(cfg, start=False)
    app.config.update(TESTING=True)
    app.extensions["abb"].store.init()
    return app


def csrf_of(client):
    page = client.get("/")
    return page.data.split(b'name="csrf-token" content="')[1].split(b'"')[0].decode()


def test_settings_page_admin_gate(tmp_path):
    app = settings_app(tmp_path, log_admin_users=frozenset({"admin"}))
    c = app.test_client()
    assert c.get("/settings", headers={"X-authentik-username": "mallory"}).status_code == 403
    r = c.get("/settings", headers={"X-authentik-username": "admin"})
    assert r.status_code == 200 and b"Gemini API key" in r.data


def test_save_applies_live_and_never_echoes_secret(tmp_path):
    app = settings_app(tmp_path)
    svc = app.extensions["abb"]
    c = app.test_client()
    assert not svc.rank.enabled

    r = c.post("/settings", data={"csrf_token": csrf_of(c),
                                  "GEMINI_API_KEY": "sk-super-secret-123",
                                  "RANK_MODEL": "gemini-3.5-flash",
                                  "WANTED_ROUTE": "default"})
    assert r.status_code == 302

    svc = app.extensions["abb"]
    assert svc.rank.enabled                        # applied without a restart
    assert svc.config.gemini_api_key == "sk-super-secret-123"

    page = c.get("/settings")
    assert b"sk-super-secret-123" not in page.data  # write-only
    assert b"set in app" in page.data               # provenance chip
    assert "•".encode() in page.data                # "(set)" placeholder


def test_reset_returns_to_env(tmp_path):
    app = settings_app(tmp_path)
    svc = app.extensions["abb"]
    c = app.test_client()
    token = csrf_of(c)
    c.post("/settings", data={"csrf_token": token, "GEMINI_API_KEY": "sk-x",
                              "WANTED_ROUTE": "default"})
    assert app.extensions["abb"].rank.enabled
    # A partial POST (only the reset) must not invent overrides for absent fields.
    c.post("/settings", data={"csrf_token": token, "reset_GEMINI_API_KEY": "on"})
    svc = app.extensions["abb"]
    assert not svc.rank.enabled                    # back to (unset) env
    assert svc.store.settings_all() == {}


def test_bool_and_float_fields(tmp_path):
    app = settings_app(tmp_path)
    c = app.test_client()
    c.post("/settings", data={"csrf_token": csrf_of(c),
                              "present_WANTED_AUTO_DOWNLOAD": "1",
                              "WANTED_AUTO_DOWNLOAD": "on",
                              "ABS_LOW_KBPS": "48",
                              "WANTED_ROUTE": "tor"})
    cfg = app.extensions["abb"].config
    assert cfg.wanted_auto_download is True
    assert cfg.abs_low_kbps == 48.0
    assert cfg.wanted_route == "tor"


def test_invalid_float_is_rejected_with_message(tmp_path):
    app = settings_app(tmp_path)
    c = app.test_client()
    r = c.post("/settings", data={"csrf_token": csrf_of(c),
                                  "ABS_LOW_KBPS": "very fast",
                                  "WANTED_ROUTE": "default"},
               follow_redirects=True)
    assert b"must be a number" in r.data
    assert app.extensions["abb"].config.abs_low_kbps == 63.0


def test_settings_need_the_data_volume(client):
    # The default test app has LOG_DB_PATH="" -> store disabled.
    r = client.get("/settings")
    assert r.status_code == 200
    assert b"can't persist" in r.data


def test_boot_report_reflects_app_settings(tmp_path):
    """The startup config report must describe the EFFECTIVE config: a Gemini
    key living in app settings reads as enabled, not 'disabled (no
    GEMINI_API_KEY)' — that exact confusion happened on a real deployment."""
    app = settings_app(tmp_path)
    svc = app.extensions["abb"]
    svc.store.settings_set("GEMINI_API_KEY", "sk-app-key", None, "admin")
    svc.reload_settings()

    report = "\n".join(svc.config.report())
    assert "Smart sort: enabled" in report
    assert "sk-app-key" not in report                     # still masked
    assert sum(1 for v in svc.settings_provenance.values() if v == "app") == 1
