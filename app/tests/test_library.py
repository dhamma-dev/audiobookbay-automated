"""The ownership join and Upgrade Radar logic, exercised against a fake index —
no Audiobookshelf, no network."""

from abb.library import AbsLibrary, norm_seq, parse_kbps
from tests.conftest import make_config


def lib():
    # abs_url/token set so `enabled` reads True; nothing here calls the API.
    return AbsLibrary(make_config(abs_url="http://abs.local", abs_token="t"))


def owned(title, author, series=None, est_kbps=None, tracks=0):
    return {"title": title, "author": author, "series": series or [],
            "asin": "", "isbn": "", "language": "",
            "size": 1, "duration": 1, "tracks": tracks, "est_kbps": est_kbps}


INDEX = [
    owned("Unsouled", "Will Wight", [("Cradle", "1")]),
    owned("Soulsmith", "Will Wight", [("Cradle", "2")], est_kbps=40, tracks=30),
    owned("Dune", "Frank Herbert"),
]


def test_helpers():
    assert norm_seq("3.0") == "3" and norm_seq(2) == "2"
    assert parse_kbps("128 kbps") == 128.0 and parse_kbps("Unknown") is None


def test_quality_flag_precision_first():
    L = lib()
    assert L.quality_flag(owned("x", "a")) is None                    # unknown -> no flag
    assert "kbps" in L.quality_flag(owned("x", "a", est_kbps=40))
    assert "files" in L.quality_flag(owned("x", "a", tracks=30))
    assert L.quality_flag(owned("x", "a", est_kbps=128)) is None


def test_is_upgrade_result_gates():
    L = lib()
    bad_copy = owned("x", "a", est_kbps=40)
    assert L.is_upgrade_result({"is_m4b": True, "bitrate": "128 kbps"}, bad_copy)
    assert not L.is_upgrade_result({"is_m4b": False, "bitrate": "128 kbps"}, bad_copy)
    assert not L.is_upgrade_result({"is_m4b": True, "bitrate": "32 kbps"}, bad_copy)
    good_copy = owned("x", "a", est_kbps=128)
    assert not L.is_upgrade_result({"is_m4b": True, "bitrate": "320 kbps"}, good_copy)


def test_owned_series_fuzzy_join():
    L = lib()
    series_index = L.owned_series_index(INDEX)
    # Exact and fuzzy series names both resolve.
    assert set(L.owned_seqs_for("Cradle", series_index)) == {"1", "2"}
    assert set(L.owned_seqs_for("The Cradle Series", series_index)) == {"1", "2"}
    assert L.owned_seqs_for("Wheel of Time", series_index) == {}


def test_resolve_ownership_canonical_series_and_collections():
    L = lib()
    ranking = {
        "canonical": [
            {"id": 0, "title": "Dune", "author": "Frank Herbert"},
            {"id": 1, "title": "Project Hail Mary", "author": "Andy Weir"},
        ],
        "series": [{
            "label": "Cradle",
            "entries": [
                # owned entry: best + alt inherit ownership
                {"seq": 1, "title": "Unsouled", "best_id": 2, "alt_ids": [3]},
                # not owned: no claim
                {"seq": 3, "title": "Blackflame", "best_id": 4},
            ],
            "collections": [
                {"id": 5, "title": "Cradle 1-2", "covers": [1, 2]},   # fully owned
                {"id": 6, "title": "Cradle 1-3", "covers": [1, 2, 3]},  # partial
            ],
        }],
        "editions": [],
    }
    results = [{"id": 2, "format": "M4B", "bitrate": "128 kbps"}]
    L.resolve_ownership(ranking, INDEX, results)

    by_id = {o["id"]: o for o in ranking["ownership"]}
    assert by_id[0]["status"] == "owned"          # canonical standalone
    assert 1 not in by_id                          # unowned -> silence, never "no"
    assert by_id[2]["status"] == "owned"          # series best
    assert by_id[3]["status"] == "owned"          # alt inherits
    assert 4 not in by_id
    assert by_id[5]["status"] == "owned"          # omnibus fully covered
    assert by_id[6] == {"id": 6, "status": "partial", "detail": "2 of 3"}


def test_resolve_ownership_reports_upgrade_for_flagged_copy():
    L = lib()
    ranking = {"canonical": [{"id": 0, "title": "Soulsmith", "author": "Will Wight",
                              "series": "Cradle", "seq": 2}],
               "series": [], "editions": []}
    results = [{"id": 0, "format": "M4B", "bitrate": "128 kbps"}]
    L.resolve_ownership(ranking, INDEX, results)
    (o,) = ranking["ownership"]
    assert o["status"] == "upgrade" and "kbps" in o["detail"]


def test_resolve_ownership_never_raises():
    L = lib()
    L.resolve_ownership({"series": [{"entries": [{"seq": None}]}], "canonical": None},
                        INDEX)  # malformed ranking -> best-effort, no exception
    L.resolve_ownership(None, INDEX)
    L.resolve_ownership({}, [])


def test_annotate_matches_disabled_is_noop():
    disabled = AbsLibrary(make_config())  # no ABS_URL/TOKEN
    books = [{"title": "Dune - Frank Herbert"}]
    disabled.annotate_matches(books)
    assert "library_match" not in books[0]
    assert disabled.get_index() == []
