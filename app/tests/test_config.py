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
