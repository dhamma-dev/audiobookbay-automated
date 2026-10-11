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
    # there's no torrent behind them, so they must never become a pick.
    books = [
        {"title": "Dune - Frank Herbert"},
        {"title": "(REQ) Dune - Frank Herbert"},
        {"title": "Children of Time - Adrian Tchaikovsky"},  # different work
    ]
    hits = WantedService._match_against(books, "Dune", "Frank Herbert")
    assert [h["title"] for h in hits] == ["Dune - Frank Herbert"]


def test_match_against_refuses_sequels():
    # This deterministic pick can auto-download (no key / API failure), and the
    # README promises "strict title+author". The token-set matcher used to
    # accept every "... of Dune" for a wanted "Dune" and could send whichever
    # ranked highest by format/bitrate; the identity guards now refuse them.
    books = [
        {"title": "God Emperor of Dune - Frank Herbert", "is_m4b": True},
        {"title": "Dune Messiah - Frank Herbert"},
        {"title": "Dune (Dune Chronicles #1) - Frank Herbert"},
    ]
    hits = WantedService._match_against(books, "Dune", "Frank Herbert")
    assert [h["title"] for h in hits] == ["Dune (Dune Chronicles #1) - Frank Herbert"]


def test_match_against_keeps_subtitled_wanted_titles():
    # Hardcover titles carry subtitles the ABB listing omits; the broad query
    # ladder relies on still matching them.
    hits = WantedService._match_against([{"title": "The Last Wish - Andrzej Sapkowski"}],
                                        "The Last Wish: Introducing the Witcher",
                                        "Andrzej Sapkowski")
    assert len(hits) == 1


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
    """Owned books as {title: author} (a set means author-less items). The
    sweep does real matcher passes against get_index(); owns() serves the
    pre-search and add-time checks."""

    def __init__(self, owned, enabled=True):
        self.enabled = enabled
        self.owned = dict(owned) if isinstance(owned, dict) else {t: "" for t in owned}
        self.get_index_calls = []   # records max_age per call, for assertions

    def _items(self):
        return [{"title": t, "author": a, "series": [], "asin": "", "isbn": "",
                 "language": ""} for t, a in self.owned.items()]

    def get_index(self, max_age=None):
        self.get_index_calls.append(max_age)
        return self._items()

    def peek_index(self):
        return self._items()

    def owns(self, title, author):
        return title in self.owned


def test_owned_sweep_flips_rows_that_landed(tmp_path):
    from abb.storage import Store
    cfg = make_config(log_db_path=str(tmp_path / "w.db"), hardcover_api_key="k")
    store = Store(cfg)
    store.init()
    library = FakeLibrary({"I Know Why the Caged Bird Sings": "Maya Angelou"})
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
    library.owned["Not Landed Yet"] = "Someone"
    svc._sweep_owned()
    assert {r["hc_id"]: r for r in store.wanted_rows()}[2]["status"] == "sent"
    svc._last_owned_sweep = None                   # TTL elapsed (never-ran sentinel)
    svc._sweep_owned()
    assert {r["hc_id"]: r for r in store.wanted_rows()}[2]["status"] == "owned"


class IdLibrary(FakeLibrary):
    """Index items carry Audiobookshelf ids, like the real index."""

    def __init__(self, items):
        super().__init__({})
        self.items = items

    def _items(self):
        return [dict(it) for it in self.items]


def _lib_item(item_id, title, author):
    return {"id": item_id, "title": title, "author": author, "series": [],
            "asin": "", "isbn": "", "language": ""}


def test_reopen_ignores_only_the_rejected_match(tmp_path):
    """"Search anyway" on a done row: back in the queue, and the library item
    that claimed it is ignored by the sweep — but a genuinely different copy
    landing later still flips it (the matcher stays in charge)."""
    from abb.storage import Store
    cfg = make_config(log_db_path=str(tmp_path / "w.db"), hardcover_api_key="k")
    store = Store(cfg)
    store.init()
    library = IdLibrary([_lib_item("li_wrong", "Red Rising", "Pierce Brown")])
    svc = WantedService(cfg, store, None, library, None, None, None, None)
    store.wanted_upsert({"hc_id": 1, "title": "Red Rising", "author": "Pierce Brown",
                         "status": "owned"})

    assert svc.reopen(1, "alice") == (True, "")
    (row,) = store.wanted_rows()
    assert row["status"] == "wanted" and row["searched_at"] is None
    assert "alice" in row["detail"]
    assert {r["hc_id"] for r in svc.due_rows()} == {1}   # searched right away

    svc._sweep_owned()                                   # rejected item: no re-flip
    assert store.wanted_rows()[0]["status"] == "wanted"
    library.items.append(_lib_item("li_real", "Red Rising", "Pierce Brown"))
    svc._last_owned_sweep = None
    svc._sweep_owned()                                   # a real copy still counts
    assert store.wanted_rows()[0]["status"] == "owned"

    store.wanted_upsert({"hc_id": 2, "title": "Queued", "status": "wanted"})
    assert svc.reopen(2, "alice")[0] is False            # only done rows reopen
    assert svc.reopen(99, "alice")[0] is False


def test_done_shelf_is_rechecked_once_and_flags_not_reopens(tmp_path):
    """Rows an older, looser matcher filed as owned (a sequel read as book 1)
    are flagged on the worker's first sweep — flagged, never reopened
    automatically: that could re-download a book the user does own."""
    from abb.storage import Store
    from abb.wanted import UNCONFIRMED
    cfg = make_config(log_db_path=str(tmp_path / "w.db"), hardcover_api_key="k")
    store = Store(cfg)
    store.init()
    library = FakeLibrary({"Dune": "Frank Herbert"})
    svc = WantedService(cfg, store, None, library, None, None, None, None)
    store.wanted_upsert({"hc_id": 1, "title": "Dune Messiah", "author": "Frank Herbert",
                         "status": "owned", "detail": "in your library 2026-07-01"})
    store.wanted_upsert({"hc_id": 2, "title": "Dune", "author": "Frank Herbert",
                         "status": "owned", "detail": "in your library 2026-07-01"})

    svc._sweep_owned()
    rows = {r["hc_id"]: r for r in store.wanted_rows()}
    assert rows[1]["status"] == "owned" and rows[1]["detail"].startswith(UNCONFIRMED)
    assert rows[2]["detail"] == "in your library 2026-07-01"   # confirmed: untouched

    # Once per worker: later sweeps don't redo the pass.
    store.wanted_upsert({"hc_id": 1, "detail": "manually cleared"})
    svc._last_owned_sweep = None
    svc._sweep_owned()
    assert {r["hc_id"]: r for r in store.wanted_rows()}[1]["detail"] == "manually cleared"


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
    svc = WantedService(cfg, store, None,
                        FakeLibrary({"Skipped But Owned": "Some Author"}),
                        None, None, None, None)
    store.wanted_upsert({"hc_id": 1, "title": "Skipped But Owned",
                         "author": "Some Author", "status": "skipped"})
    svc._sweep_owned()
    assert store.wanted_rows()[0]["status"] == "owned"


class FakeScraper:
    def __init__(self, books):
        self.books = books
        self.sessions = []   # (call, sess) — which route each ABB request took

    def search(self, query, max_pages=5, sess=None):
        self.sessions.append(("search", sess))
        return [dict(b) for b in self.books]

    def extract_magnet_link(self, link, sess=None, title=None):
        self.sessions.append(("magnet", sess))
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


def test_inflight_guard_prevents_duplicate_searches(tmp_path):
    """A quick-add's inline search and the worker tick must not both search
    the same row (observed live: double scrape, double Gemini verdict)."""
    svc, store, clients = autodownload_service(tmp_path, M4B_BOOK)
    row = store.wanted_rows()[0]

    # Someone else (the inline search) is mid-flight on this row.
    svc._inflight.add(row["hc_id"])
    assert svc.search_one(row) == "in-flight"
    assert store.wanted_rows()[0]["status"] == "wanted"   # untouched
    svc._inflight.discard(row["hc_id"])

    # A normal search registers and always deregisters, even on success.
    assert svc.search_one(row) == "found"
    assert svc._inflight == set()


def _routed_service(tmp_path, tor_status, **overrides):
    """A WantedService wired to a real Outbound over a stub Tor, so routing
    decisions are the production ones."""
    import time
    from abb.outbound import Outbound
    from abb.storage import Store
    from tests.test_tor import StubTor
    kwargs = dict(log_db_path=str(tmp_path / "w.db"), hardcover_api_key="k")
    kwargs.update(overrides)
    cfg = make_config(**kwargs)
    store = Store(cfg)
    store.init()
    tor = StubTor(tor_status)
    outbound = Outbound(cfg, tor)
    if tor_status == "ready":
        tor.on_ready()
    scraper, clients = FakeScraper([M4B_BOOK]), FakeClients()
    svc = WantedService(cfg, store, scraper, FakeLibrary(set()), FakeRank(), clients,
                        outbound, tor)
    svc.last_sync = time.monotonic()   # _tick: skip the Hardcover sync
    store.wanted_upsert({"hc_id": 1, "title": "Dune", "author": "Frank Herbert",
                         "status": "wanted"})
    return svc, store, scraper, outbound


def test_background_searches_pause_instead_of_going_direct(tmp_path):
    import pytest
    from abb.outbound import TorUnavailable
    for overrides in ({"use_tor": True}, {"use_tor": False, "wanted_route": "tor"}):
        svc, store, scraper, _ob = _routed_service(tmp_path / str(len(overrides)),
                                                   "unavailable", **overrides)
        assert svc.background_paused() == "unavailable"
        with pytest.raises(TorUnavailable):
            svc._session()                    # WANTED_ROUTE=tor used to mean "or direct"
        svc._tick()
        assert scraper.sessions == []         # nothing went out, least of all direct
        assert store.wanted_rows()[0]["searched_at"] is None   # still due, waiting


def test_direct_background_route_does_not_wait_for_tor(tmp_path):
    svc, _store, scraper, ob = _routed_service(tmp_path, "starting", wanted_route="direct",
                                               use_tor=True)
    assert svc.background_paused() is None
    svc._tick()
    assert scraper.sessions[0] == ("search", ob.direct_session)


def test_auto_send_fetches_the_magnet_on_the_search_route(tmp_path):
    """USE_TOR=false + WANTED_ROUTE=tor: search AND detail page go via Tor.
    The magnet fetch used to take the server default (Direct) instead."""
    svc, store, scraper, ob = _routed_service(tmp_path, "ready", use_tor=False,
                                              wanted_route="tor", wanted_auto_download=True)
    svc._tick()
    assert scraper.sessions == [("search", ob.tor_session), ("magnet", ob.tor_session)]
    (entry,) = store.fetch_download_log()
    assert entry["status"] == "ok" and entry["route"] == "tor"


# --- verdict memory: unchanged results never go back to the AI ----------------------
class RecordingRank:
    """An active AI verdict that records the listings it was shown and answers
    from a script (default: none of them is the book)."""
    enabled = True

    def __init__(self, answer=None):
        self.calls = []
        self.answer = answer or (lambda listings: {
            "match_found": False, "ranked": [], "notes": [], "reason": "not this book"})

    def wanted_verdict(self, title, author, listings):
        self.calls.append([li["title"] for li in listings])
        return self.answer(listings)


def listing(n, **fields):
    return dict({"title": f"Unrelated Listing {n} - Someone",
                 "link": f"https://audiobookbay.lu/abss/unrelated-{n}/", "format": "MP3",
                 "bitrate": "64 Kbps", "size": "300 MB", "language": "English",
                 "keywords": [], "is_m4b": False}, **fields)


def memory_service(tmp_path, books, rank=None, **overrides):
    from abb.storage import Store
    kwargs = dict(log_db_path=str(tmp_path / "w.db"), hardcover_api_key="k")
    kwargs.update(overrides)
    cfg = make_config(**kwargs)
    store = Store(cfg)
    store.init()
    scraper, rank = FakeScraper(books), rank or RecordingRank()
    svc = WantedService(cfg, store, scraper, FakeLibrary(set()), rank, FakeClients(),
                        FakeOutbound(), None)
    store.wanted_upsert({"hc_id": 1, "title": "Obscure Book", "author": "Some Author",
                         "status": "wanted"})
    return svc, store, scraper, rank


def row_of(store):
    return store.wanted_rows()[0]


def test_unchanged_results_are_not_sent_to_the_ai_again(tmp_path):
    svc, store, _scraper, rank = memory_service(tmp_path, [listing(1), listing(2), listing(3)])
    assert svc.search_one(row_of(store)) == "unmatched"
    # One call: the ladder's second query returned the same listings, and
    # those were already judged moments earlier.
    assert len(rank.calls) == 1 and len(rank.calls[0]) == 3

    assert svc.search_one(row_of(store)) == "unmatched"      # the next daily check
    assert len(rank.calls) == 1                               # nothing new, no call
    detail = row_of(store)["detail"]
    assert "nothing new since the AI last looked" in detail and "AI: not this book" in detail


def test_only_new_listings_are_sent_and_can_still_be_found(tmp_path):
    svc, store, scraper, rank = memory_service(tmp_path, [listing(1), listing(2)])
    svc.search_one(row_of(store))
    scraper.books.append(listing(3, title="Obscure Book - Some Author", format="M4B",
                                 is_m4b=True))
    rank.answer = lambda listings: {"match_found": True, "ranked": [0], "notes": [],
                                    "reason": "the new upload is the book"}
    assert svc.search_one(row_of(store)) == "found"
    assert rank.calls[-1] == ["Obscure Book - Some Author"]   # only the unseen listing
    assert row_of(store)["best_title"] == "Obscure Book - Some Author"


def test_the_recheck_button_gets_a_fresh_look(tmp_path):
    svc, store, _scraper, rank = memory_service(tmp_path, [listing(1), listing(2)])
    svc.search_one(row_of(store))
    svc.search_one(row_of(store), fresh=True)                 # the ↻ button
    assert len(rank.calls) == 2 and len(rank.calls[1]) == 2   # everything, again


def test_an_edited_post_is_judged_again(tmp_path):
    svc, store, scraper, rank = memory_service(tmp_path, [listing(1, title="Obscure Book (sample)")])
    svc.search_one(row_of(store))
    scraper.books[0] = listing(1, title="Obscure Book - Some Author")   # same post, updated
    svc.search_one(row_of(store))
    assert rank.calls[-1] == ["Obscure Book - Some Author"]


def test_memory_resets_when_the_book_model_or_language_changes(tmp_path):
    from dataclasses import replace
    svc, store, _scraper, rank = memory_service(tmp_path, [listing(1)])
    svc.search_one(row_of(store))
    for change in (lambda: store.wanted_upsert({"hc_id": 1, "title": "Obscure Book: Revised"}),
                   lambda: setattr(svc, "config", replace(svc.config, rank_model="gemini-9")),
                   lambda: setattr(svc, "config", replace(svc.config, preferred_language="German"))):
        before = len(rank.calls)
        change()
        svc.search_one(row_of(store))
        assert len(rank.calls) == before + 1   # a different judge or book: ask again


def test_memory_expires(tmp_path):
    import json
    from datetime import datetime, timedelta, timezone
    from abb.wanted import VERDICT_MEMORY_DAYS
    svc, store, _scraper, rank = memory_service(tmp_path, [listing(1)])
    svc.search_one(row_of(store))
    memory = json.loads(row_of(store)["verdict_cache"])
    old = datetime.now(timezone.utc) - timedelta(days=VERDICT_MEMORY_DAYS)
    memory["since"] = old.isoformat(timespec="seconds")
    store.wanted_upsert({"hc_id": 1, "verdict_cache": json.dumps(memory)})
    svc.search_one(row_of(store))
    assert len(rank.calls) == 2   # a mistaken "no" can't stick past the window


def test_failed_or_garbled_verdicts_are_not_remembered(tmp_path):
    answers = (lambda listings: None,                                 # timeout / quota
               lambda listings: {"match_found": True, "ranked": [99],    # no valid pick
                                 "notes": [], "reason": "?"})
    for case, answer in enumerate(answers):
        svc, store, _scraper, rank = memory_service(tmp_path / f"case{case}",
                                                    [listing(1)], RecordingRank(answer))
        svc.search_one(row_of(store))
        assert row_of(store)["verdict_cache"] is None
        svc.search_one(row_of(store))
        assert len(rank.calls) >= 2   # asked again next time


def test_the_fallback_never_revives_a_listing_the_ai_ruled_out(tmp_path):
    # The AI rejected a listing the deterministic matcher would accept (say, an
    # abridged sample). If a later AI call fails, the fallback must not pick
    # it — and auto-download it — behind the AI's back.
    lookalike = listing(1, title="Obscure Book - Some Author", format="M4B", is_m4b=True)
    svc, store, scraper, rank = memory_service(tmp_path, [lookalike])
    svc.search_one(row_of(store))                  # AI: not this book
    scraper.books.append(listing(2))
    rank.answer = lambda listings: None            # the next call fails
    assert svc.search_one(row_of(store)) == "unmatched"


def test_judgments_survive_a_search_cut_short(tmp_path):
    # AI judged the first query's listings, then ABB stopped answering: those
    # verdicts still stand and aren't paid for again.
    svc, store, scraper, rank = memory_service(tmp_path, [listing(1), listing(2)])
    answers = iter([[listing(1), listing(2)], None])
    scraper.search = lambda q, max_pages=5, sess=None: next(answers)
    assert svc.search_one(row_of(store)) == "unreachable"
    scraper.search = lambda q, max_pages=5, sess=None: [listing(1), listing(2)]
    svc.search_one(row_of(store))
    assert len(rank.calls) == 1


def test_search_anyway_clears_the_memory(tmp_path):
    svc, store, _scraper, rank = memory_service(tmp_path, [listing(1)])
    svc.search_one(row_of(store))
    store.wanted_upsert({"hc_id": 1, "status": "owned"})
    svc.reopen(1, "alice")
    assert row_of(store)["verdict_cache"] is None
    svc.search_one(row_of(store))
    assert len(rank.calls) == 2   # a fresh look after "this was wrong"


def test_memory_is_off_when_the_ai_is(tmp_path):
    rank = RecordingRank()
    rank.enabled = False                            # no key / WANTED_LLM=false
    svc, store, _scraper, _rank = memory_service(tmp_path, [listing(1)], rank)
    svc.search_one(row_of(store))
    svc.search_one(row_of(store))
    assert row_of(store)["verdict_cache"] is None   # deterministic, exactly as before


def test_memory_is_capped():
    import json
    from abb.wanted import VERDICT_MEMORY_MAX
    memory = {"stamp": "s", "since": "2026-09-01T00:00:00+00:00",
              "keys": [f"old{i}" for i in range(VERDICT_MEMORY_MAX)], "reason": "r"}
    stored = json.loads(WantedService._remembered("s", memory, ["new1", "new2"], "", "now"))
    assert len(stored["keys"]) == VERDICT_MEMORY_MAX
    assert stored["keys"][-2:] == ["new1", "new2"] and "old0" not in stored["keys"]
    assert stored["reason"] == "r" and stored["since"] == memory["since"]


def test_auto_policy_label(tmp_path):
    svc, _, _ = autodownload_service(tmp_path, M4B_BOOK)
    assert svc.auto_policy_label() == "M4B only"
    svc.config = make_config(hardcover_api_key="k", wanted_auto_format="any",
                             wanted_auto_min_kbps=100.0)
    assert svc.auto_policy_label() == "any format, ≥ 100 kbps"


def test_worker_sweep_demands_a_fresh_index(tmp_path):
    """The sweep must not wait out the full 15-minute ABS cache on top of its
    own cadence — it asks for an index at most OWNED_SWEEP_TTL old."""
    from abb.storage import Store
    from abb.wanted import OWNED_SWEEP_TTL
    cfg = make_config(log_db_path=str(tmp_path / "w.db"), hardcover_api_key="k")
    store = Store(cfg)
    store.init()
    library = FakeLibrary({})
    svc = WantedService(cfg, store, None, library, None, None, None, None)
    svc._sweep_owned()
    assert library.get_index_calls == [OWNED_SWEEP_TTL]
    svc.sweep_owned_now()   # Sync now's companion forces an even fresher one
    assert library.get_index_calls[-1] == 30


def test_cached_sweep_is_local_only(tmp_path):
    """The page-load sweep flips rows using only the in-memory index — it
    never triggers a fetch (get_index is not called at all)."""
    from abb.storage import Store
    cfg = make_config(log_db_path=str(tmp_path / "w.db"), hardcover_api_key="k")
    store = Store(cfg)
    store.init()
    library = FakeLibrary({"Treasure Island": "Robert Louis Stevenson"})
    svc = WantedService(cfg, store, None, library, None, None, None, None)
    store.wanted_upsert({"hc_id": 1, "title": "Treasure Island",
                         "author": "Robert Louis Stevenson", "status": "sent"})
    svc.sweep_owned_cached()
    assert library.get_index_calls == []                     # local only
    assert store.wanted_rows()[0]["status"] == "owned"


def test_sweep_borrows_author_from_the_sent_pick(tmp_path):
    """A quick-add without an author would otherwise never flip (title-only
    caps at MAYBE) — the sweep borrows the author from the listing that was
    actually sent."""
    from abb.storage import Store
    cfg = make_config(log_db_path=str(tmp_path / "w.db"), hardcover_api_key="k")
    store = Store(cfg)
    store.init()
    library = FakeLibrary({"Treasure Island": "Robert Louis Stevenson"})
    svc = WantedService(cfg, store, None, library, None, None, None, None)
    store.wanted_upsert({"hc_id": -1, "title": "Treasure island", "author": "",
                         "status": "sent", "added_by": "dhamma",
                         "best_title": "Treasure Island - Robert Louis Stevenson"})
    svc.sweep_owned_now()
    assert store.wanted_rows()[0]["status"] == "owned"


def test_auto_send_backfills_the_row_author(tmp_path):
    svc, store, clients = autodownload_service(tmp_path, M4B_BOOK)
    store.wanted_upsert({"hc_id": 1, "author": ""})   # quick-add style: no author

    class VerdictRank:  # the AI picks it (deterministic matching can't, sans author)
        def wanted_verdict(self, title, author, listings):
            return {"match_found": True, "ranked": [0], "notes": [], "reason": "ok"}

    svc.rank = VerdictRank()
    svc.search_and_autodownload(store.wanted_rows()[0])
    (row,) = store.wanted_rows()
    assert row["status"] == "sent"
    assert row["author"] == "Frank Herbert"   # borrowed from the sent listing
