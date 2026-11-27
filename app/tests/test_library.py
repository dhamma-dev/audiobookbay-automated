"""The ownership join and Upgrade Radar logic, exercised against a fake index —
no Audiobookshelf, no network."""

import pytest
import requests

from abb.library import AbsLibrary, AbsWriteError, norm_seq, parse_kbps
from tests.conftest import make_config


ADMINS = frozenset({"boss"})
USER_HEADER = "X-authentik-username"


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


def test_canonical_fallback_never_resolves_to_a_sibling():
    # Canonical "Dune" #1 misses the (series, seq) join when only #2 is owned;
    # the fuzzy fallback used to hand back the owned sibling anyway.
    L = lib()
    index = [owned("Dune Messiah", "Frank Herbert", [("Dune", "2")])]
    series_index = L.owned_series_index(index)
    book1 = {"title": "Dune", "author": "Frank Herbert", "series": "Dune", "seq": 1}
    assert L.canonical_owned(book1, index, series_index) is None
    book2 = {"title": "Dune Messiah", "author": "Frank Herbert", "series": "Dune", "seq": 2}
    assert L.canonical_owned(book2, index, series_index) is index[0]


def test_load_items_keeps_the_subtitle():
    L = lib()
    L._get = lambda path, **params: {"results": [{"media": {"metadata": {
        "title": "Atomic Habits", "subtitle": "An Easy & Proven Way",
        "authorName": "James Clear"}}}]}
    (item,) = L._load_items("lib")
    assert item["subtitle"] == "An Easy & Proven Way"


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


def test_parse_series_field_reads_the_list_endpoints_series_name():
    """The ABS library list is always the minified form: no `series` array,
    only a `seriesName` display string. Reading just the array left every
    owned book seriesless (live: Regicide, Completionist Chronicles #2, never
    showed as owned)."""
    from abb.matching import parse_series_field as p
    assert p({"seriesName": "The Completionist Chronicles #2"}) == \
        [("The Completionist Chronicles", "2")]
    assert p({"seriesName": "Cradle #1, Will Wight Universe #2.5"}) == \
        [("Cradle", "1"), ("Will Wight Universe", "2.5")]
    # A comma inside a series name doesn't split it.
    assert p({"seriesName": "Me, Myself and I #3"}) == [("Me, Myself and I", "3")]
    assert p({"seriesName": "Discworld"}) == [("Discworld", None)]
    assert p({"seriesName": ""}) == [] and p({}) == []
    # Expanded array, and the single object a series filter adds, still work.
    assert p({"series": [{"name": "Dune", "sequence": "1"}]}) == [("Dune", "1")]
    assert p({"series": {"name": "Dune", "sequence": "2"}}) == [("Dune", "2")]


def test_load_items_gives_series_numbers_from_minified_list(monkeypatch):
    L = lib()
    page = {"results": [{"id": "li_1", "media": {
        "metadata": {"title": "Regicide", "authorName": "Dakota Krout",
                     "seriesName": "The Completionist Chronicles #2"},
        "size": 100, "duration": 10, "numTracks": 1}}], "total": 1}
    monkeypatch.setattr(L, "_get", lambda path, **params: page)
    (item,) = L._load_items("lib")
    assert item["series"] == [("The Completionist Chronicles", "2")]

    index = [item]
    series_index = L.owned_series_index(index)
    # Smart sort's shelf join, and the deterministic "Book 2" badge.
    assert set(L.owned_seqs_for("Completionist Chronicles", series_index)) == {"2"}
    book = {"title": "The Completionist Chronicles, Book 2: Regicide - Dakota Krout"}
    monkeypatch.setattr(L, "get_index", lambda max_age=None: index)
    L.annotate_matches([book])
    assert book["library_match"]["title"] == "Regicide"


def test_shelf_entry_title_fallback_and_owned_gaps():
    L = lib()
    # Owned copy with NO series metadata: the (series, seq) join can't see it,
    # the author-gated title match can. And a gap you own is reported.
    index = [owned("Ritualist", "Dakota Krout"),
             owned("Regicide", "Dakota Krout", [("The Completionist Chronicles", "2")])]
    ranking = {"series": [{"label": "The Completionist Chronicles", "author": "Dakota Krout",
                           "entries": [{"seq": 1, "title": "Ritualist", "best_id": 0},
                                       {"seq": 2, "title": "Regicide"},
                                       {"seq": 3, "title": "Rexus"},
                                       {"seq": 4, "title": "Raze", "best_id": 1}]}]}
    L.resolve_ownership(ranking, index, [])
    assert [o["id"] for o in ranking["ownership"]] == [0]
    (gap,) = ranking["owned_gaps"]
    assert (gap["series"], gap["seq"], gap["match"]["title"]) == (0, 2, "Regicide")
    assert "label" not in gap["match"]          # never offered unless asked for


def test_unnumbered_owned_copy_exact_title_and_suspected():
    """Series numbers are often never filled in on the Audiobookshelf side."""
    L = lib()
    index = [dict(owned("Regicide", "Dakota Krout"), id="li_2"),
             dict(owned("He Who Fights with Monsters", "Shirtaloon"), id="li_h")]
    si = L.owned_series_index(index)
    # Smart sort knows the series, and the title is the book's own name:
    # confirmed, number or no number.
    c = {"title": "Regicide", "author": "Dakota Krout",
         "series": "The Completionist Chronicles", "seq": 2}
    assert L.canonical_lookup(c, index, si) == (index[0], True)
    # A title that is just the series name could be any volume: suspected only.
    c5 = {"title": "He Who Fights with Monsters", "author": "Shirtaloon",
          "series": "He Who Fights with Monsters", "seq": 5}
    assert L.canonical_lookup(c5, index, si) == (index[1], False)
    assert L.canonical_owned(c5, index, si) is None

    # Raw titles (no smart sort): the series name is unknown, so a "Book 2"
    # against an unnumbered copy is suspected, with the copy shown.
    L.get_index = lambda max_age=None: index
    books = [{"title": "The Completionist Chronicles, Book 2: Regicide - Dakota Krout"},
             {"title": "Regicide - Dakota Krout"}]
    L.annotate_matches(books)
    assert books[0]["library_match"]["suspected"] is True
    assert books[0]["library_match"]["title"] == "Regicide"
    assert "suspected" not in books[1]["library_match"]
    # A sequel whose title merely contains the owned one is not even suspected.
    index.append(owned("Dune", "Frank Herbert"))
    sequel = [{"title": "Dune Messiah, Book 2 - Frank Herbert"}]
    L.annotate_matches(sequel)
    assert "library_match" not in sequel[0]
    index.pop()

    ranking = {"canonical": [dict(c5, id=7)], "series": []}
    L.resolve_ownership(ranking, index, [])
    assert ranking["ownership"] == [{"id": 7, "status": "suspected", "match": {
        "title": "He Who Fights with Monsters", "author": "Shirtaloon", "series": "",
        "url": "http://abs.local/item/li_h"}}]


def test_match_info_link_only_for_browser_reachable_hosts():
    item = dict(owned("Soulsmith", "Will Wight", [("Cradle", "2.0")]), id="li_9")
    assert lib().match_info(item) == {"title": "Soulsmith", "author": "Will Wight",
                                      "series": "Cradle #2",
                                      "url": "http://abs.local/item/li_9"}
    docker = AbsLibrary(make_config(abs_url="http://audiobookshelf:80", abs_token="t"))
    assert "url" not in docker.match_info(item)


# --- Series labels: the one write to Audiobookshelf -----------------------------

def test_label_proposal_fills_blanks_only():
    L = lib()
    bare = dict(owned("Regicide", "Dakota Krout"), id="li_2")
    assert L.label_proposal(bare, "The Completionist Chronicles", 2.0) == {
        "item": "li_2", "series": "The Completionist Chronicles", "seq": "2"}
    # In the series with no number: keep Audiobookshelf's own name for it.
    unnumbered = dict(bare, series=[("Completionist Chronicles", None)])
    assert L.label_proposal(unnumbered, "The Completionist Chronicles", 2)["series"] == \
        "Completionist Chronicles"
    # Already numbered there (even differently): not ours to change.
    numbered = dict(bare, series=[("Completionist Chronicles", "3")])
    assert L.label_proposal(numbered, "The Completionist Chronicles", 2) is None
    # Nothing to write without an id, a name, or a real number.
    assert L.label_proposal(owned("Regicide", "x"), "S", 2) is None
    assert L.label_proposal(bare, "", 2) is None
    assert L.label_proposal(bare, "S", None) is None
    assert L.label_proposal(bare, "S", "two") is None


def test_shelf_offers_labels_only_when_asked():
    L = lib()
    index = [dict(owned("Ritualist", "Dakota Krout"), id="li_1"),
             dict(owned("Regicide", "Dakota Krout"), id="li_2"),
             dict(owned("Rexus", "Dakota Krout", [("The Completionist Chronicles", "3")]), id="li_3")]
    def ranking():
        return {"series": [{"label": "The Completionist Chronicles", "author": "Dakota Krout",
                            "entries": [{"seq": 1, "title": "Ritualist", "best_id": 0},
                                        {"seq": 2, "title": "Regicide"},
                                        {"seq": 3, "title": "Rexus", "best_id": 1}]}]}
    r = ranking()
    L.resolve_ownership(r, index, [], offer_labels=True)
    by_id = {o["id"]: o for o in r["ownership"]}
    assert by_id[0]["match"]["label"] == {
        "item": "li_1", "series": "The Completionist Chronicles", "seq": "1"}
    assert "label" not in by_id[1]["match"]            # already numbered in ABS
    assert r["owned_gaps"][0]["match"]["label"]["item"] == "li_2"
    r = ranking()
    L.resolve_ownership(r, index, [])
    assert all("label" not in o["match"] for o in r["ownership"])


def _abs_stub(L, live_series, calls, patch_error=None):
    L._get = lambda path, **kw: {"media": {"metadata": {"series": live_series}}}

    def patch(path, body):
        calls.append((path, body))
        if patch_error:
            raise patch_error
        return {"updated": True}
    L._patch = patch


def test_set_series_sends_the_whole_list_and_updates_the_cache():
    L = lib()
    item = dict(owned("Regicide", "Dakota Krout", [("Krout Universe", "7")]), id="li_2")
    L.get_index = lambda max_age=None: [item]
    calls = []
    # ABS replaces the whole list on update: the existing series must go back too.
    _abs_stub(L, [{"id": "s1", "name": "Krout Universe", "sequence": "7"}], calls)
    assert L.set_series("li_2", "The Completionist Chronicles", 2.0) == ("set", item)
    assert calls == [("/api/items/li_2/media", {"metadata": {"series": [
        {"name": "Krout Universe", "sequence": "7"},
        {"name": "The Completionist Chronicles", "sequence": "2"}]}})]
    assert item["series"] == [("Krout Universe", "7"), ("The Completionist Chronicles", "2")]
    assert L.match_info(item)["series"] == "Krout Universe #7, The Completionist Chronicles #2"


def test_set_series_never_overwrites_a_number():
    L = lib()
    item = dict(owned("Regicide", "Dakota Krout"), id="li_2")   # stale cache: no series
    L.get_index = lambda max_age=None: [item]
    calls = []
    # Someone numbered it in ABS since the cache was filled.
    _abs_stub(L, [{"id": "s1", "name": "Completionist Chronicles", "sequence": "2"}], calls)
    assert L.set_series("li_2", "The Completionist Chronicles", 5)[0] == "already"
    assert calls == [] and item["series"] == [("Completionist Chronicles", "2")]
    # In the series, number blank: the number is filled under ABS's own name.
    _abs_stub(L, [{"id": "s1", "name": "Completionist Chronicles", "sequence": None}], calls)
    assert L.set_series("li_2", "The Completionist Chronicles", 2)[0] == "set"
    assert calls[0][1] == {"metadata": {"series": [
        {"name": "Completionist Chronicles", "sequence": "2"}]}}


def test_set_series_errors_are_plain():
    L = lib()
    item = dict(owned("Regicide", "Dakota Krout"), id="li_2")
    L.get_index = lambda max_age=None: [item]
    with pytest.raises(AbsWriteError):
        L.set_series("nope", "S", 1)
    with pytest.raises(AbsWriteError):
        L.set_series("li_2", "S", "two")
    resp = requests.Response()
    resp.status_code = 403
    _abs_stub(L, [], [], patch_error=requests.HTTPError(response=resp))
    with pytest.raises(AbsWriteError, match="update permission"):
        L.set_series("li_2", "S", 1)
    assert item["series"] == []                       # cache untouched on failure


def test_series_label_endpoint_is_admin_only():
    from abb import create_app
    app = create_app(make_config(abs_url="http://abs.local", abs_token="t",
                                 log_admin_users=ADMINS), start=False)
    s = app.extensions["abb"]
    item = dict(owned("Regicide", "Dakota Krout"), id="li_2")
    s.library.get_index = lambda max_age=None: [item]
    calls = []
    _abs_stub(s.library, [], calls)
    c = app.test_client()
    body = {"item": "li_2", "series": "The Completionist Chronicles", "seq": 2}
    r = c.post("/api/library/series", json=body, headers={USER_HEADER: "guest"})
    assert r.status_code == 403 and calls == []
    r = c.post("/api/library/series", json=body, headers={USER_HEADER: "boss"})
    assert r.get_json() == {"outcome": "set", "match": {
        "title": "Regicide", "author": "Dakota Krout",
        "series": "The Completionist Chronicles #2", "url": "http://abs.local/item/li_2"}}
    r = c.post("/api/library/series", json=dict(body, item="nope"), headers={USER_HEADER: "boss"})
    assert r.status_code == 502 and "isn't in" in r.get_json()["message"]
