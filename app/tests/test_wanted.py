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


def test_skip_and_unskip_lifecycle(tmp_path):
    from abb.storage import Store
    cfg = make_config(log_db_path=str(tmp_path / "w.db"), hardcover_api_key="k")
    store = Store(cfg)
    store.init()
    svc = WantedService(cfg, store, None, None, None, None, None, None)

    store.wanted_upsert({"hc_id": 1, "title": "Obscure Book", "status": "unmatched",
                         "searched_at": "2026-01-01T00:00:00+00:00"})
    store.wanted_upsert({"hc_id": 2, "title": "Found Book", "status": "found"})

    ok, _ = svc.skip(1)
    assert ok
    (row,) = [r for r in store.wanted_rows() if r["hc_id"] == 1]
    assert row["status"] == "skipped" and "skipped" in row["detail"]

    # Out of every queue: not due, and neither sync-requeue nor boot-requeue
    # touch it.
    assert svc.due_rows() == []
    svc.requeue_unresolved()
    svc.requeue_open()
    (row,) = [r for r in store.wanted_rows() if r["hc_id"] == 1]
    assert row["status"] == "skipped" and row["searched_at"] is not None

    # Guards: found rows can't be skipped; non-skipped rows can't be unskipped.
    ok, msg = svc.skip(2)
    assert not ok and "searched" in msg
    ok, _ = svc.unskip(2)
    assert not ok
    ok, _ = svc.skip(99)
    assert not ok

    # Allowing again puts it back in the queue, due immediately.
    ok, _ = svc.unskip(1)
    assert ok
    (row,) = [r for r in store.wanted_rows() if r["hc_id"] == 1]
    assert row["status"] == "wanted" and row["searched_at"] is None
    assert {r["hc_id"] for r in svc.due_rows()} == {1}


def test_owned_sweep_covers_skipped_rows(tmp_path):
    # A skipped book that lands in the library is done — ownership wins.
    from abb.storage import Store
    cfg = make_config(log_db_path=str(tmp_path / "w.db"), hardcover_api_key="k")
    store = Store(cfg)
    store.init()
    svc = WantedService(cfg, store, None, FakeLibrary({"Skipped But Owned"}),
                        None, None, None, None)
    store.wanted_upsert({"hc_id": 1, "title": "Skipped But Owned", "status": "skipped"})
    svc._sweep_owned()
    assert store.wanted_rows()[0]["status"] == "owned"


class FakeScraper:
    def __init__(self, books):
        self.books = books

    def search(self, query, max_pages=5, sess=None):
        return [dict(b) for b in self.books]

    def extract_magnet_link(self, link):
        return "magnet:?xt=urn:btih:abc123&tr=x"


class FakeRank:
    enabled = False

    def wanted_verdict(self, title, author, listings):
        return None  # force the deterministic pick


class FakeClients:
    ok = True

    def __init__(self):
        self.added = []

    def add(self, magnet, title):
        self.added.append((magnet, title))


class FakeOutbound:
    def route_mode(self):
        return "direct"


def autodownload_service(tmp_path, book, **overrides):
    from abb.storage import Store
    kwargs = dict(log_db_path=str(tmp_path / "w.db"), hardcover_api_key="k",
                  wanted_auto_download=True)
    kwargs.update(overrides)
    cfg = make_config(**kwargs)
    store = Store(cfg)
    store.init()
    clients = FakeClients()
    svc = WantedService(cfg, store, FakeScraper([book]), FakeLibrary(set()),
                        FakeRank(), clients, FakeOutbound(), None)
    store.wanted_upsert({"hc_id": 1, "title": "Dune", "author": "Frank Herbert",
                         "status": "wanted"})
    return svc, store, clients


M4B_BOOK = {"title": "Dune - Frank Herbert", "link": "https://audiobookbay.lu/abss/dune/",
            "format": "M4B", "bitrate": "128 kbps", "size": "500 MB",
            "language": "English", "keywords": [], "is_m4b": True}


def test_autodownload_fires_from_any_discovery_path(tmp_path):
    """The manual re-check uses the same search_and_autodownload as the
    worker — 'auto-download on' means on regardless of who found the book."""
    svc, store, clients = autodownload_service(tmp_path, M4B_BOOK)
    row = store.wanted_rows()[0]
    status = svc.search_and_autodownload(row)   # what /wanted/research/<id> calls
    assert status == "found"

    (magnet, title) = clients.added[0]          # the transfer actually went out
    assert magnet.startswith("magnet:") and "Dune" in title
    (fresh,) = store.wanted_rows()
    assert fresh["status"] == "sent" and fresh["detail"] == "auto-downloaded"
    (log_row,) = store.fetch_download_log()
    assert log_row["user"] == "hardcover-auto" and log_row["status"] == "ok"


def test_autodownload_skip_is_explained_not_silent(tmp_path):
    mp3 = dict(M4B_BOOK, format="MP3", is_m4b=False)
    svc, store, clients = autodownload_service(tmp_path, mp3)
    svc.search_and_autodownload(store.wanted_rows()[0])

    assert clients.added == []                   # gate held: not M4B
    (row,) = store.wanted_rows()
    assert row["status"] == "found"              # still ready for a manual Send
    assert "isn't M4B" in row["detail"]          # ...and the row says why


def test_autodownload_off_leaves_found_alone(tmp_path):
    svc, store, clients = autodownload_service(tmp_path, M4B_BOOK)
    svc.config = make_config(log_db_path=svc.config.log_db_path,
                             hardcover_api_key="k", wanted_auto_download=False)
    svc.search_and_autodownload(store.wanted_rows()[0])
    assert clients.added == []
    assert store.wanted_rows()[0]["status"] == "found"


def test_add_manual_checks_library_and_duplicates(tmp_path):
    from abb.storage import Store
    cfg = make_config(log_db_path=str(tmp_path / "w.db"), hardcover_api_key="k")
    store = Store(cfg)
    store.init()
    svc = WantedService(cfg, store, None, FakeLibrary({"Owned Already"}),
                        None, None, None, None)

    # Owned -> told immediately, nothing stored.
    assert svc.add_manual("Owned Already", "Someone", "alice") == ("owned", None)
    assert store.wanted_rows() == []

    # Fresh add -> negative id, credited, queued.
    outcome, hc_id = svc.add_manual("New Book", "New Author", "alice")
    assert outcome == "added" and hc_id == -1
    (row,) = store.wanted_rows()
    assert row["status"] == "wanted" and row["added_by"] == "alice"

    # Duplicate (case/punctuation-insensitive) -> refused; ids keep descending.
    assert svc.add_manual("new book!", "New Author", "bob") == ("duplicate", None)
    outcome, hc_id = svc.add_manual("Another Book", "", "bob")
    assert outcome == "added" and hc_id == -2

    # A Hardcover row with the same title also counts as a duplicate.
    store.wanted_upsert({"hc_id": 500, "title": "From Hardcover", "author": "X",
                         "status": "wanted"})
    assert svc.add_manual("From Hardcover", "X", "alice") == ("duplicate", None)


def test_manual_add_autodownloads_as_the_user(tmp_path):
    """Fire-and-forget: manual rows auto-send even with the master switch
    OFF, and the download log credits the requesting user. (Format allowed
    here by the server policy — the gate itself is universal, tested below.)"""
    mp3 = dict(M4B_BOOK, format="MP3", is_m4b=False,
               title="Dune - Frank Herbert")
    svc, store, clients = autodownload_service(tmp_path, mp3)
    svc.config = make_config(log_db_path=svc.config.log_db_path,
                             hardcover_api_key="k", wanted_auto_download=False,
                             wanted_auto_format="any")
    store.wanted_delete_missing(set())  # drop the fixture's hc_id=1 row
    outcome, hc_id = svc.add_manual("Dune", "Frank Herbert", "alice")
    assert outcome == "added"

    row = next(r for r in store.wanted_rows() if r["hc_id"] == hc_id)
    status = svc.search_and_autodownload(row)   # what /wanted/add runs inline
    assert status == "found"
    assert len(clients.added) == 1              # sent despite MP3 + global auto OFF
    fresh = next(r for r in store.wanted_rows() if r["hc_id"] == hc_id)
    assert fresh["status"] == "sent"
    (log_row,) = store.fetch_download_log()
    assert log_row["user"] == "alice"           # on behalf of the requester


def test_sync_never_deletes_manual_rows(tmp_path):
    from abb.storage import Store
    cfg = make_config(log_db_path=str(tmp_path / "w.db"), hardcover_api_key="k")
    store = Store(cfg)
    store.init()
    svc = WantedService(cfg, store, None, FakeLibrary(set()), None, None, None, None)
    svc.add_manual("Manual Book", "A", "alice")
    store.wanted_upsert({"hc_id": 7, "title": "HC Book", "status": "wanted"})

    svc._fetch_wanted = lambda: []   # Hardcover list emptied
    svc.sync_list()
    titles = {r["title"] for r in store.wanted_rows()}
    assert titles == {"Manual Book"}  # HC row gone, manual row untouched


def test_remove_manual_only(tmp_path):
    from abb.storage import Store
    cfg = make_config(log_db_path=str(tmp_path / "w.db"), hardcover_api_key="k")
    store = Store(cfg)
    store.init()
    svc = WantedService(cfg, store, None, FakeLibrary(set()), None, None, None, None)
    _, hc_id = svc.add_manual("Mistake", "", "alice")
    store.wanted_upsert({"hc_id": 7, "title": "HC Book", "status": "wanted"})

    ok, msg = svc.remove_manual(7)
    assert not ok and "Hardcover" in msg
    ok, _ = svc.remove_manual(hc_id)
    assert ok
    assert {r["title"] for r in store.wanted_rows()} == {"HC Book"}


def test_auto_gate_is_universal_manual_rows_included(tmp_path):
    """One server policy: with the default m4b requirement, a manual add whose
    best pick is MP3 is held for review — same rule as Hardcover rows."""
    mp3 = dict(M4B_BOOK, format="MP3", is_m4b=False)
    svc, store, clients = autodownload_service(tmp_path, mp3,
                                               wanted_auto_download=False)
    store.wanted_delete(1)  # drop the fixture's default row (same title)
    _, hc_id = svc.add_manual("Dune", "Frank Herbert", "alice")
    row = next(r for r in store.wanted_rows() if r["hc_id"] == hc_id)
    svc.search_and_autodownload(row)

    assert clients.added == []
    fresh = next(r for r in store.wanted_rows() if r["hc_id"] == hc_id)
    assert fresh["status"] == "found" and "isn't M4B" in fresh["detail"]


def test_auto_gate_minimum_bitrate(tmp_path):
    # 64 kbps M4B pick vs a 100 kbps minimum -> held, with the numbers named.
    low = dict(M4B_BOOK, bitrate="64 Kbps")
    svc, store, clients = autodownload_service(tmp_path, low,
                                               wanted_auto_min_kbps=100.0)
    svc.search_and_autodownload(store.wanted_rows()[0])
    assert clients.added == []
    (row,) = store.wanted_rows()
    assert row["status"] == "found" and "below the 100 kbps minimum" in row["detail"]

    # An unknown bitrate passes — blocking on missing metadata would strand
    # too many legitimate picks.
    unknown = dict(M4B_BOOK, bitrate="Unknown")
    svc2, store2, clients2 = autodownload_service(tmp_path.joinpath("u"), unknown,
                                                  wanted_auto_min_kbps=100.0)
    svc2.search_and_autodownload(store2.wanted_rows()[0])
    assert len(clients2.added) == 1
    assert store2.wanted_rows()[0]["status"] == "sent"


def test_auto_format_config_parsing():
    from abb.config import Config
    from abb.settings import coerce
    assert Config.from_env({}).wanted_auto_format == "m4b"
    assert Config.from_env({"WANTED_AUTO_FORMAT": "ANY"}).wanted_auto_format == "any"
    assert Config.from_env({"WANTED_AUTO_FORMAT": "flac"}).wanted_auto_format == "m4b"
    assert coerce("WANTED_AUTO_FORMAT", "any", "m4b") == "any"
    assert coerce("WANTED_AUTO_FORMAT", "nonsense", "m4b") == "m4b"
