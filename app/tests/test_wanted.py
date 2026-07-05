from abb.wanted import (WantedService, candidate_payload, parse_hardcover_wanted,
                        wanted_queries)
from tests.conftest import make_config


def test_query_ladder_broad_first():
    # Subtitle and volume designators are cut so ABB's AND-ish search can't
    # zero out; the surname-narrowed query is the fallback.
    assert wanted_queries("The Way of Kings (The Stormlight Archive, Book 1)",
                          "Brandon Sanderson") == \
        ["the way of kings", "the way of kings sanderson"]
    assert wanted_queries("The Witcher, Vol. 1", "Andrzej Sapkowski") == \
        ["the witcher", "the witcher sapkowski"]


def test_query_ladder_generic_title_leads_narrowed():
    # An ultra-generic stem ("It") matches everything — the narrowed query leads.
    assert wanted_queries("It", "Stephen King") == ["it king", "it"]


def test_query_ladder_no_author():
    assert wanted_queries("Project Hail Mary", "") == ["project hail mary"]


def test_parse_hardcover_payload():
    data = {"user_books": [
        {"book": {"id": 11, "title": "Dune", "slug": "dune",
                  "contributions": [{"author": {"name": "Frank Herbert"}}]}},
        {"book": {"id": 12, "title": "Untitled?", "slug": "",
                  "contributions": []}},
        {"book": {"title": "No id -> dropped"}},
        {"book": {}},
    ]}
    rows = parse_hardcover_wanted(data)
    assert rows == [
        {"hc_id": 11, "title": "Dune", "author": "Frank Herbert", "slug": "dune"},
        {"hc_id": 12, "title": "Untitled?", "author": "", "slug": ""},
    ]


def _service(**config_overrides):
    """A WantedService with only what these unit tests touch (config)."""
    cfg = make_config(**config_overrides)
    return WantedService(cfg, None, None, None, None, None, None, None)


def test_deterministic_rank_prefers_m4b_language_bitrate():
    svc = _service(preferred_language="English")
    hits = [
        {"title": "mp3 high", "is_m4b": False, "language": "English", "bitrate": "320 kbps"},
        {"title": "m4b german", "is_m4b": True, "language": "German", "bitrate": "128 kbps"},
        {"title": "m4b english low", "is_m4b": True, "language": "English", "bitrate": "64 kbps"},
        {"title": "m4b english high", "is_m4b": True, "language": "English", "bitrate": "128 kbps"},
    ]
    ranked = svc._rank_deterministic(hits)
    assert [h["title"] for h in ranked] == [
        "m4b english high", "m4b english low", "m4b german", "mp3 high"]


def test_match_against_skips_request_posts():
    # Request posts ("(REQ) ...") describe a book somebody is ASKING for —
    # there's no torrent behind them, so they must never become a pick. Note
    # the token-set matcher is deliberately extra-word tolerant ("Dune
    # Messiah" would still match "Dune") — identity strictness is the AI
    # verdict's job; this deterministic pass is the broad fallback.
    books = [
        {"title": "Dune - Frank Herbert"},
        {"title": "(REQ) Dune - Frank Herbert"},
        {"title": "Children of Time - Adrian Tchaikovsky"},  # different work
    ]
    hits = WantedService._match_against(books, "Dune", "Frank Herbert")
    assert [h["title"] for h in hits] == ["Dune - Frank Herbert"]


def test_candidate_payload_is_slim():
    book = {"title": "T", "link": "L", "format": "M4B", "bitrate": "128",
            "size": "1 GB", "language": "English", "is_m4b": True,
            "cover": "should-not-persist", "keywords": ["x"]}
    slim = candidate_payload(book)
    assert "cover" not in slim and "keywords" not in slim
    assert slim["is_m4b"] is True and slim["title"] == "T"


def test_due_rows_and_requeue(tmp_path):
    from abb.storage import Store
    cfg = make_config(log_db_path=str(tmp_path / "w.db"), hardcover_api_key="k")
    store = Store(cfg)
    store.init()
    svc = WantedService(cfg, store, None, None, None, None, None, None)

    store.wanted_upsert({"hc_id": 1, "title": "Never searched", "status": "wanted"})
    store.wanted_upsert({"hc_id": 2, "title": "Settled", "status": "found",
                         "searched_at": "2026-01-01T00:00:00+00:00"})
    store.wanted_upsert({"hc_id": 3, "title": "Old unmatched", "status": "unmatched",
                         "searched_at": "2026-01-01T00:00:00+00:00"})
    store.wanted_upsert({"hc_id": 4, "title": "Sent", "status": "sent",
                         "searched_at": "2026-01-01T00:00:00+00:00"})

    due = {r["hc_id"] for r in svc.due_rows()}
    assert due == {1, 3}  # found/sent/owned rows are settled

    svc.requeue_unresolved()
    row3 = next(r for r in store.wanted_rows() if r["hc_id"] == 3)
    row2 = next(r for r in store.wanted_rows() if r["hc_id"] == 2)
    assert row3["searched_at"] is None       # open row requeued
    assert row2["searched_at"] is not None   # found row untouched


def test_mark_sent_by_link_matches_pick_and_alternatives(tmp_path):
    import json
    from abb.storage import Store
    cfg = make_config(log_db_path=str(tmp_path / "w.db"), hardcover_api_key="k")
    store = Store(cfg)
    store.init()
    svc = WantedService(cfg, store, None, None, None, None, None, None)

    store.wanted_upsert({
        "hc_id": 1, "title": "Dune", "status": "found",
        "best_link": "https://abb/pick",
        "candidates": json.dumps([{"link": "https://abb/pick"},
                                  {"link": "https://abb/alt"}])})
    svc.mark_sent_by_link("https://abb/alt")  # user chose the alternative
    (row,) = store.wanted_rows()
    assert row["status"] == "sent"


class FakeLibrary:
    def __init__(self, owned_titles, enabled=True):
        self.enabled = enabled
        self.owned_titles = set(owned_titles)

    def owns(self, title, author):
        return title in self.owned_titles


def test_owned_sweep_flips_rows_that_landed(tmp_path):
    from abb.storage import Store
    cfg = make_config(log_db_path=str(tmp_path / "w.db"), hardcover_api_key="k")
    store = Store(cfg)
    store.init()
    library = FakeLibrary({"I Know Why the Caged Bird Sings"})
    svc = WantedService(cfg, store, None, library, None, None, None, None)

    store.wanted_upsert({"hc_id": 1, "title": "I Know Why the Caged Bird Sings",
                         "author": "Maya Angelou", "status": "sent"})
    store.wanted_upsert({"hc_id": 2, "title": "Not Landed Yet",
                         "author": "Someone", "status": "sent"})
    svc._sweep_owned()

    rows = {r["hc_id"]: r for r in store.wanted_rows()}
    assert rows[1]["status"] == "owned"            # the download arrived
    assert "in your library" in rows[1]["detail"]
    assert rows[2]["status"] == "sent"             # no match -> no flip

    # The sweep is throttled: within its TTL a new arrival waits for the next pass.
    library.owned_titles.add("Not Landed Yet")
    svc._sweep_owned()
    assert {r["hc_id"]: r for r in store.wanted_rows()}[2]["status"] == "sent"
    svc._last_owned_sweep = None                   # TTL elapsed (never-ran sentinel)
    svc._sweep_owned()
    assert {r["hc_id"]: r for r in store.wanted_rows()}[2]["status"] == "owned"


def test_owned_sweep_noop_without_abs(tmp_path):
    from abb.storage import Store
    cfg = make_config(log_db_path=str(tmp_path / "w.db"), hardcover_api_key="k")
    store = Store(cfg)
    store.init()
    svc = WantedService(cfg, store, None, FakeLibrary({"X"}, enabled=False),
                        None, None, None, None)
    store.wanted_upsert({"hc_id": 1, "title": "X", "status": "sent"})
    svc._sweep_owned()
    assert store.wanted_rows()[0]["status"] == "sent"  # matching is off -> no claims
