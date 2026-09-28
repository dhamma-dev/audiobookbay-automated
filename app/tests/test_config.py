from abb.config import Config


def test_defaults_from_empty_env():
    c = Config.from_env({})
    assert c.abb_hostname == "audiobookbay.lu"
    assert c.request_timeout == 45.0
    assert c.use_tor and c.tor_autostart
    assert c.rank_thinking_budget == 0
    assert not c.smart_sort_enabled and not c.abs_enabled and not c.wanted_enabled
    assert c.log_enabled  # the log defaults ON at /data/downloads.db


def test_timeout_and_thinking_parsing():
    assert Config.from_env({"REQUEST_TIMEOUT": "off"}).request_timeout is None
    assert Config.from_env({"REQUEST_TIMEOUT": "0"}).request_timeout is None
    assert Config.from_env({"REQUEST_TIMEOUT": "90"}).request_timeout == 90.0
    assert Config.from_env({"RANK_THINKING_BUDGET": "-1"}).rank_thinking_budget is None
    assert Config.from_env({"RANK_THINKING_BUDGET": "256"}).rank_thinking_budget == 256
    assert Config.from_env({}).gemini_timeout == 60.0
    assert Config.from_env({"GEMINI_TIMEOUT": ""}).gemini_timeout == 60.0  # compose's empty unset
    assert Config.from_env({"GEMINI_TIMEOUT": "off"}).gemini_timeout is None
    assert Config.from_env({"GEMINI_TIMEOUT": "90"}).gemini_timeout == 90.0


def test_blank_env_values_fall_back_to_defaults():
    # docker-compose passes listed-but-unset keys as "" — a blank must behave
    # like an unset key, not beat the default or crash a number.
    c = Config.from_env({"DL_CATEGORY": "", "ABS_LOW_KBPS": "", "TOR_SOCKS_PORT": " ",
                         "RANK_MODEL": "", "USE_TOR": "", "ABB_HOSTNAME": ""})
    assert c.dl_category == "Audiobookbay-Audiobooks"
    assert c.abs_low_kbps == 63.0 and c.tor_socks_port == 9050
    assert c.rank_model == "gemini-3.5-flash" and c.use_tor
    assert c.abb_hostname == "audiobookbay.lu"
    # ...except the documented v1 switch: an empty LOG_DB_PATH turns the log off.
    assert not Config.from_env({"LOG_DB_PATH": ""}).log_enabled


def test_the_shipped_compose_file_boots_with_nothing_set():
    """Emulate compose's substitution over docker-compose.yaml with no .env:
    `${KEY}` -> "", `${KEY:-default}` -> default. Every default must hold."""
    import os
    import re
    path = os.path.join(os.path.dirname(__file__), "..", "..", "docker-compose.yaml")
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"\s*-\s*([A-Z_]+)=\$\{[A-Z_]+(?::-([^}]*))?\}", line)
            if m:
                env[m.group(1)] = m.group(2) or ""
    assert "DL_CATEGORY" in env and env["DL_CATEGORY"] == ""   # the case that bit
    c = Config.from_env(env)
    assert c.dl_category == "Audiobookbay-Audiobooks"
    assert c.use_tor and c.log_enabled and c.gemini_timeout == 60.0


def test_dl_url_parsing():
    c = Config.from_env({"DL_URL": "https://torrents.local:8112"})
    assert (c.dl_scheme, c.dl_host, c.dl_port) == ("https", "torrents.local", "8112")
    # host+port synthesize a URL for Deluge
    c = Config.from_env({"DL_HOST": "10.0.0.2", "DL_PORT": "8112"})
    assert c.dl_url == "http://10.0.0.2:8112"


def test_client_validation_messages():
    ok, err = Config.from_env({}).validate_client()
    assert not ok and "DOWNLOAD_CLIENT" in err

    ok, err = Config.from_env({"DOWNLOAD_CLIENT": "floppynet"}).validate_client()
    assert not ok and "floppynet" in err

    ok, err = Config.from_env({"DOWNLOAD_CLIENT": "qbittorrent",
                               "DL_HOST": "h", "DL_PORT": "1"}).validate_client()
    assert not ok and "DL_USERNAME" in err and "DL_PASSWORD" in err

    ok, err = Config.from_env({"DOWNLOAD_CLIENT": "putio"}).validate_client()
    assert ok  # put.io readiness is the in-app banner's job


def test_report_masks_secrets():
    c = Config.from_env({
        "DOWNLOAD_CLIENT": "qbittorrent", "DL_HOST": "h", "DL_PORT": "1",
        "DL_USERNAME": "u", "DL_PASSWORD": "hunter2",
        "GEMINI_API_KEY": "sk-google-123", "ABS_TOKEN": "abs-tok",
        "ABS_URL": "http://abs.local", "HARDCOVER_API_KEY": "hc-tok",
        "PUTIO_ACCESS_TOKEN": "putio-tok",
    })
    text = "\n".join(c.report())
    for secret in ("hunter2", "sk-google-123", "abs-tok", "hc-tok", "putio-tok"):
        assert secret not in text


def test_language_matches():
    c = Config.from_env({"PREFERRED_LANGUAGE": "English"})
    assert c.language_matches({"language": "english"})
    assert c.language_matches({"language": "Eng"})
    assert c.language_matches({"language": ""})       # unknown -> don't penalize
    assert not c.language_matches({"language": "German"})
    assert Config.from_env({}).language_matches({"language": "German"})
