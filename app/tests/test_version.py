"""Build identity: the footer, /healthz and the boot log say which commit is
deployed. Images carry it as APP_* env (CI build args); local runs ask git."""

import shutil

import pytest

from abb import create_app, version
from tests.conftest import make_config

SHA = "b68986b0123456789abcdef0123456789abcdef0"
ENV = {"APP_GIT_SHA": SHA, "APP_GIT_REF": "dev", "APP_BUILD_DATE": "2026-09-28T15:05:12Z",
       "APP_GIT_REPO_URL": "https://github.com/dhamma-dev/audiobookbay-automated"}


def test_build_info_from_the_image_env():
    info = version.from_env(ENV)
    assert (info["sha"], info["full_sha"], info["ref"]) == ("b68986b", SHA, "dev")
    assert info["built"] == "2026-09-28 15:05 UTC"
    assert info["url"] == "https://github.com/dhamma-dev/audiobookbay-automated/commit/" + SHA
    assert "dev@b68986b" in version.describe(info)
    assert version.from_env({}) is None
    assert version.from_env({"APP_GIT_SHA": ""}) is None   # a plain local `docker build`


def test_footer_and_healthz_show_the_deployed_commit(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    c = create_app(make_config(), start=False).test_client()
    page = c.get("/").data.decode()
    assert 'class="footer-build"' in page and "<code>b68986b</code>" in page
    assert "/commit/" + SHA in page and "2026-09-28 15:05 UTC" in page
    assert c.get("/healthz").get_json()["version"] == {
        "commit": SHA, "ref": "dev", "built": "2026-09-28 15:05 UTC"}


def test_without_build_info_nothing_is_claimed(monkeypatch):
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    c = create_app(make_config(), start=False).test_client()
    assert 'class="footer-build"' not in c.get("/").data.decode()
    assert c.get("/healthz").get_json()["version"] is None


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_local_checkout_fallback():
    info = version.from_git_checkout()
    if info is None:
        pytest.skip("not running from a git checkout")
    assert len(info["full_sha"]) == 40 and info["source"] == "checkout"
    assert info["url"] == ""   # a local tree may not be that commit; no link
