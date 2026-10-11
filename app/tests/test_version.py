"""Build identity: the footer, /healthz and the boot log say which commit is
deployed. Images carry it in build-info.json (CI build args); local runs ask
git."""

import json
import shutil

import pytest

from abb import create_app, version
from tests.conftest import make_config

SHA = "b68986b0123456789abcdef0123456789abcdef0"
INFO = {"sha": SHA, "ref": "dev", "built": "2026-09-28T15:05:12Z",
        "repo_url": "https://github.com/dhamma-dev/audiobookbay-automated"}


@pytest.fixture
def build_file(tmp_path, monkeypatch):
    path = tmp_path / "build-info.json"
    monkeypatch.setattr(version, "BUILD_INFO_PATH", str(path))
    return path


def test_build_info_from_the_image_file(build_file):
    build_file.write_text(json.dumps(INFO))
    info = version.from_file()
    assert (info["sha"], info["full_sha"], info["ref"]) == ("b68986b", SHA, "dev")
    assert info["built"] == "2026-09-28 15:05 UTC"
    assert info["url"] == "https://github.com/dhamma-dev/audiobookbay-automated/commit/" + SHA
    assert "dev@b68986b" in version.describe(info)


@pytest.mark.parametrize("content", [
    None,                                                   # no file: a local run
    json.dumps({"sha": "", "ref": "", "built": "", "repo_url": ""}),  # plain `docker build`
    "not json", "[]",
])
def test_missing_or_empty_build_file_claims_nothing(build_file, content):
    if content is not None:
        build_file.write_text(content)
    assert version.from_file() is None


def test_footer_and_healthz_show_the_deployed_commit(build_file, monkeypatch):
    build_file.write_text(json.dumps(INFO))
    # What Portainer's "Recreate" carries over from an older image: ignored.
    monkeypatch.setenv("APP_GIT_SHA", "a6abb60" + "0" * 33)
    c = create_app(make_config(), start=False).test_client()
    page = c.get("/").data.decode()
    assert 'class="footer-build"' in page and "<code>b68986b</code>" in page
    assert "/commit/" + SHA in page and "2026-09-28 15:05 UTC" in page
    assert "a6abb60" not in page
    assert c.get("/healthz").get_json()["version"] == {
        "commit": SHA, "ref": "dev", "built": "2026-09-28 15:05 UTC"}


def test_without_build_info_nothing_is_claimed(build_file):
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
