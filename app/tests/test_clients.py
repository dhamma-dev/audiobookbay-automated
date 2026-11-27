"""Download-client adapters: a refused add must surface as an error, not as
"Download added" (qBittorrent reports refusals in the response body)."""

import pytest
from qbittorrentapi import Conflict409Error
from qbittorrentapi.torrents import TorrentsAddedMetadata

from abb.clients import ClientRegistry, qbt_add_failed
from tests.conftest import make_config

MAGNET = "magnet:?xt=urn:btih:" + "ab" * 20


def test_qbt_add_result_shapes():
    assert qbt_add_failed("Fails.")                          # Web API < 2.14
    assert not qbt_add_failed("Ok.")
    assert qbt_add_failed(TorrentsAddedMetadata({"failure_count": 1, "success_count": 0}))
    assert not qbt_add_failed(TorrentsAddedMetadata({"failure_count": 0, "success_count": 1}))
    assert not qbt_add_failed(TorrentsAddedMetadata({"failure_count": 0, "pending_count": 1}))
    assert not qbt_add_failed(TorrentsAddedMetadata({}))      # unknown shape: no guessing
    assert not qbt_add_failed(None)


def qbittorrent(outcome):
    registry = ClientRegistry(make_config(download_client="qbittorrent", dl_host="h",
                                          dl_port="1", dl_username="u", dl_password="p"))

    class FakeQbt:
        def torrents_add(self, urls, save_path, category):
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    registry._qbt = FakeQbt
    return registry


@pytest.mark.parametrize("outcome", [
    "Fails.",
    TorrentsAddedMetadata({"failure_count": 1, "success_count": 0, "pending_count": 0}),
], ids=["legacy-fails", "failure-count"])
def test_a_refused_qbittorrent_add_raises(outcome):
    with pytest.raises(RuntimeError, match="refused"):
        qbittorrent(outcome).add(MAGNET, "Dune - Frank Herbert")


def test_a_duplicate_says_so():
    with pytest.raises(RuntimeError, match="already has this torrent"):
        qbittorrent(Conflict409Error()).add(MAGNET, "Dune - Frank Herbert")


@pytest.mark.parametrize("outcome", [
    "Ok.", TorrentsAddedMetadata({"failure_count": 0, "success_count": 1})], ids=["ok", "count"])
def test_an_accepted_qbittorrent_add_passes(outcome):
    qbittorrent(outcome).add(MAGNET, "Dune - Frank Herbert")   # no exception
