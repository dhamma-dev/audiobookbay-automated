"""Labs + the library series sweep: Audiobookshelf and Audible both stubbed —
no network. The store is a real SQLite file under tmp_path."""

import pytest

from abb import create_app, sweep
from abb.library import AbsWriteError
from tests.conftest import make_config

A = "Dakota Krout"
CC = "The Completionist Chronicles"
ADMIN = {"X-authentik-username": "boss"}


def book(item_id, title, author=A, series=None, subtitle="", asin=""):
    return {"id": item_id, "title": title, "subtitle": subtitle, "author": author,
            "series": series or [], "asin": asin, "isbn": "", "language": "",
            "size": 1, "duration": 1, "tracks": 1, "est_kbps": 128}


def product(asin, title, series, author=A, subtitle=""):
    return {"asin": asin, "title": title, "subtitle": subtitle,
            "authors": [{"name": author}],
            "series": [{"title": n, "sequence": q} for n, q in series]}


@pytest.fixture
def app(tmp_path):
    application = create_app(make_config(
        log_db_path=str(tmp_path / "d.db"), abs_url="http://abs.local", abs_token="t",
        log_admin_users=frozenset({"boss"})), start=False)
    s = application.extensions["abb"]
    s.store.init()
    s.index = []
    s.live = {}            # item id -> series list as ABS holds it
    s.patches = []
    s.asked = []           # every Audible request
    s.library.get_index = lambda max_age=None: s.index
    s.library.peek_index = lambda: s.index
    s.library._get = lambda path, **kw: {"media": {"metadata": {"series": [
        dict(x, id="s") for x in s.live.get(path.rsplit("/", 1)[1], [])]}}}

    def patch(path, body):
        s.patches.append((path, body))
        s.live[path.split("/")[3]] = body["metadata"]["series"]
        return {"updated": True}
    s.library._patch = patch
    s.audible = {}         # "/ASIN" or (title, author) -> response

    def audible(path="", **params):
        key = path or (params.get("title"), params.get("author"))
        s.asked.append(key)
        return s.audible.get(key, {})
    s.sweep._audible = audible
    monkey_pace(s)
    return application


def monkey_pace(s):
    s.sweep._cancel.wait = lambda t=None: False     # no real pauses in tests


@pytest.fixture
def s(app):
    return app.extensions["abb"]


# --- reading a label out of the book itself -----------------------------------------
@pytest.mark.parametrize("item, expected", [
    (book("1", "Regicide", subtitle="The Completionist Chronicles Series, Book 2"), (CC, "2")),
    (book("1", "Soulsmith", subtitle="Cradle, Book 2"), ("Cradle", "2")),
    (book("1", "Caliban's War", subtitle="Book 2 of The Expanse"), ("The Expanse", "2")),
    (book("1", "Regicide (The Completionist Chronicles, Book 2)"), (CC, "2")),
    (book("1", "Regicide: The Completionist Chronicles Book 2.5"), (CC, "2.5")),
    (book("1", "Regicide", subtitle="Book 2"), ("", "2")),
    (book("1", "Regicide", subtitle="A LitRPG Adventure"), None),
    (book("1", "Catch-22"), None),
    (book("1", "Henry IV Part 2"), None),            # a bare title is never parsed
])
def test_parse_local(item, expected):
    assert sweep.parse_local(item) == expected


def test_needs_label():
    assert sweep.needs_label(book("1", "x"))
    assert sweep.needs_label(book("1", "x", series=[(CC, None)]))
    assert not sweep.needs_label(book("1", "x", series=[(CC, "2")]))
    assert not sweep.needs_label(book("", "x"))


def test_product_series_keeps_only_real_numbers():
    p = product("B000000001", "Box", [(CC, "1-3"), ("Krout Universe", ""), (CC, "2.0")])
    assert sweep.product_series(p) == [(CC, "2")]


# --- combining the sources ------------------------------------------------------------
def test_propose_confidence_ladder():
    def aud(via, tier="", series=((CC, "2"),)):
        return {"asin": "B07FKYC3LD", "title": "Regicide", "author": A,
                "series": [list(x) for x in series], "via": via, "tier": tier}
    plain = book("1", "Regicide")
    titled = book("1", "Regicide", subtitle=f"{CC} Series, Book 2")
    wrong = book("1", "Regicide", subtitle=f"{CC}, Book 3")
    p = lambda item, a: sweep.propose(item, a, [item])
    assert p(plain, aud("id"))["confidence"] == "high"                  # exact ID
    assert p(titled, aud("search", "strong"))["confidence"] == "high"   # two sources agree
    assert p(plain, aud("search", "strong"))["confidence"] == "medium"
    assert p(plain, aud("search", "maybe"))["confidence"] == "low"
    assert p(titled, None) == {"series": CC, "seq": "2", "confidence": "medium",
                               "source": "subtitle", "note": ""}
    clash = p(wrong, aud("search", "strong"))
    assert clash["confidence"] == "low" and "#3" in clash["note"]
    assert p(plain, None) is None and p(plain, aud("id", series=())) is None
    # "Book 2" alone needs a series the book is already filed in.
    assert p(book("1", "Regicide", subtitle="Book 2"), None) is None
    filed = book("1", "Regicide", series=[("Completionist Chronicles", None)], subtitle="Book 2")
    assert p(filed, None)["series"] == "Completionist Chronicles"
    # Audible lists two series: the one the book is filed under wins.
    both = aud("id", series=(("Krout Universe", "9"), (CC, "2")))
    assert p(filed, both)["seq"] == "2" and p(plain, both)["series"] == "Krout Universe"


def test_proposal_uses_the_librarys_spelling():
    index = [book("9", "Ritualist", series=[("Completionist Chronicles", "1")]),
             book("1", "Regicide")]
    a = {"series": [[CC, "2"]], "via": "id", "tier": ""}
    assert sweep.propose(index[1], a, index)["series"] == "Completionist Chronicles"


# --- Audible lookups -------------------------------------------------------------------
def test_lookup_by_id_then_author_gated_search(s):
    s.audible["/B07FKYC3LD"] = {"product": product("B07FKYC3LD", "Regicide", [(CC, "2")])}
    got = s.sweep.lookup(book("1", "Regicide", asin="b07fkyc3ld"))
    assert (got["via"], got["series"]) == ("id", [(CC, "2")]) and s.asked == ["/B07FKYC3LD"]

    s.asked.clear()
    s.audible[("Rexus", A)] = {"products": [
        product("B000000009", "Rexus", [("Other Saga", "7")], author="Someone Else"),
        product("B000000003", "Rexus", [(CC, "3")])]}
    got = s.sweep.lookup(book("2", "Rexus"))
    assert (got["via"], got["tier"], got["asin"]) == ("search", "strong", "B000000003")
    # A different author's same-titled book is refused; no author, no search.
    s.audible[("Raze", A)] = {"products": [
        product("B000000009", "Raze", [("Other Saga", "7")], author="Someone Else")]}
    assert s.sweep.lookup(book("3", "Raze")) == {}
    assert s.sweep.lookup(book("4", "Raze", author="")) == {}


# --- the sweep itself ------------------------------------------------------------------
def test_sweep_builds_the_queue_and_caches_lookups(s):
    s.index = [book("1", "Ritualist", series=[(CC, "1")]),                  # labelled: untouched
               book("2", "Regicide", asin="B07FKYC3LD"),
               book("3", "Rexus", subtitle=f"{CC}, Book 3"),
               book("4", "Dungeon Born")]
    s.audible["/B07FKYC3LD"] = {"product": product("B07FKYC3LD", "Regicide", [(CC, "2")])}
    s.sweep.sweep("audible")
    rows, counts = s.sweep.queue()
    assert [(r["title"], r["series"], r["seq"], r["confidence"]) for r in rows] == [
        ("Regicide", CC, "2", "high"), ("Rexus", CC, "3", "medium")]
    assert counts["none"] == 1 and rows[0]["audible_url"].endswith("/B07FKYC3LD")
    assert "/B07FKYC3LD" in s.asked and ("Ritualist", A) not in s.asked
    # A second run asks Audible nothing: answers are remembered.
    s.asked.clear()
    s.sweep.sweep("audible")
    assert s.asked == []
    s.sweep.sweep("audible", recheck=True)
    assert len(s.asked) == 3


def test_subtitles_only_sends_nothing(s):
    s.index = [book("2", "Regicide", asin="B07FKYC3LD"),
               book("3", "Rexus", subtitle=f"{CC}, Book 3")]
    s.sweep.sweep("local")
    assert s.asked == []
    assert [r["title"] for r in s.sweep.queue()[0]] == ["Rexus"]


def test_apply_history_and_undo(s):
    s.index = [book("3", "Rexus", subtitle=f"{CC}, Book 3"),
               book("5", "Ra", subtitle=f"{CC}, Book 5")]
    s.live["3"] = [{"name": "Krout Universe", "sequence": "8"}]
    s.sweep.sweep("local")
    assert s.sweep.apply([{"item": "3", "series": CC, "seq": "3"},
                          {"item": "nope", "series": CC, "seq": "1"}], "boss")[:2] == (1, 0)
    assert s.live["3"] == [{"name": "Krout Universe", "sequence": "8"},
                           {"name": CC, "sequence": "3"}]
    assert [r["title"] for r in s.sweep.queue()[0]] == ["Ra"]
    (h,) = s.sweep.history()
    assert (h["user"], h["source"], h["before_text"], h["after_text"]) == (
        "boss", "sweep", "Krout Universe #8", f"Krout Universe #8, {CC} #3")
    # Undo restores the list exactly and puts the row back up for review.
    s.sweep.undo(h["id"])
    assert s.live["3"] == [{"name": "Krout Universe", "sequence": "8"}]
    assert s.index[0]["series"] == [("Krout Universe", "8")]
    assert sorted(r["title"] for r in s.sweep.queue()[0]) == ["Ra", "Rexus"]
    with pytest.raises(AbsWriteError):
        s.sweep.undo(h["id"])                         # once only


def test_undo_refuses_when_the_book_changed_since(s):
    s.index = [book("3", "Rexus", subtitle=f"{CC}, Book 3")]
    s.sweep.sweep("local")
    s.sweep.apply([{"item": "3", "series": CC, "seq": "3"}], "boss")
    s.live["3"] = [{"name": CC, "sequence": "4"}]     # someone corrected it in ABS
    with pytest.raises(AbsWriteError, match="changed"):
        s.sweep.undo(s.sweep.history()[0]["id"])
    assert s.live["3"] == [{"name": CC, "sequence": "4"}]


def test_dismissed_rows(s):
    s.index = [book("3", "Rexus", subtitle=f"{CC}, Book 3"),
               book("5", "Ra", subtitle=f"{CC}, Book 5")]
    s.sweep.sweep("local")
    s.sweep.dismiss("3", "skipped")
    s.sweep.dismiss("5", "standalone")
    assert s.sweep.queue()[0] == []
    s.sweep.sweep("local")                            # skipped returns, standalone doesn't
    assert [r["title"] for r in s.sweep.queue()[0]] == ["Rexus"]


def test_review_is_grouped_by_series_with_library_context(s):
    s.index = [book("1", "Ritualist", series=[("Completionist Chronicles", "1")]),
               book("3", "Rexus", subtitle=f"{CC}, Book 3"),
               book("2", "Regicide", subtitle=f"{CC}, Book 2"),
               book("9", "Wrong", subtitle=f"{CC}, Book 1"),          # #1 is taken
               book("u", "Unsouled", author="Will Wight", subtitle="Cradle, Book 1"),
               book("x", "Dungeon Born")]
    s.sweep.sweep("local")
    groups, counts, stats = s.sweep.grouped()
    assert [g["series"] for g in groups] == ["Completionist Chronicles", "Cradle"]
    cc = groups[0]
    # In series order, the owned book shown beside the proposals.
    assert [(e["seq"], e["title"], bool(e.get("owned"))) for e in cc["entries"]] == [
        ("1", "Ritualist", True), ("1", "Wrong", False),
        ("2", "Regicide", False), ("3", "Rexus", False)]
    assert cc["owned_count"] == 1
    assert [r["clash"] for r in cc["rows"] if r["title"] == "Wrong"] == [True]
    assert not any(r["clash"] for r in cc["rows"] if r["title"] != "Wrong")
    assert stats == {"library": 6, "numbered": 1} and counts["none"] == 1
    (none,) = s.sweep.unmatched()
    assert none["title"] == "Dungeon Born" and "Audible not asked" in none["why"]


# --- the pages ---------------------------------------------------------------------------
def form_client(app):
    """A test client whose form POSTs carry the session's CSRF token."""
    c = app.test_client()
    token = c.get("/").data.split(b'name="csrf-token" content="')[1].split(b'"')[0].decode()
    post = c.post

    def with_token(path, data=None, **kw):
        if data is not None:
            data = dict(data, csrf_token=token)
        return post(path, data=data, **kw)
    c.post = with_token
    return c


def test_labs_is_admin_only_and_off_by_default(app, s):
    c = form_client(app)
    assert c.get("/labs").status_code == 403
    assert c.get("/labs/series", headers=ADMIN).status_code == 404     # not switched on
    page = c.get("/labs", headers=ADMIN)
    assert page.status_code == 200 and b"What leaves this box" in page.data
    assert c.post("/labs/toggle", data={"key": "series_sweep", "on": "1"}).status_code == 403
    c.post("/labs/toggle", data={"key": "series_sweep", "on": "1"}, headers=ADMIN)
    assert s.labs.enabled("series_sweep")
    assert c.get("/labs/series", headers=ADMIN).status_code == 200
    assert c.get("/labs/series").status_code == 403
    assert c.post("/labs/toggle", data={"key": "nope", "on": "1"}, headers=ADMIN).status_code == 404


def test_review_page_apply_and_undo_through_the_forms(app, s):
    s.labs.set("series_sweep", True, "boss")
    s.index = [book("3", "Rexus", subtitle=f"{CC}, Book 3"),
               book("5", "Ra", subtitle=f"{CC}, Book 5")]
    s.sweep.sweep("local")
    c = form_client(app)
    page = c.get("/labs/series", headers=ADMIN).data.decode()
    assert 'name="series-3"' in page and "Rexus" in page and "data-group" in page
    assert c.get("/labs/series?tab=none", headers=ADMIN).status_code == 200
    # The number was corrected on the page before applying.
    r = c.post("/labs/series/apply", headers=ADMIN, data={
        "item": ["3"], "series-3": CC, "seq-3": "4", "series-5": CC, "seq-5": "5"})
    assert r.status_code == 302 and "Labelled+1+book" in r.headers["Location"]
    assert s.live["3"] == [{"name": CC, "sequence": "4"}] and "5" not in s.live
    c.post("/labs/series/apply", headers=ADMIN, data={"standalone": "5"})
    assert s.store.proposals()["5"]["status"] == "standalone"
    hist = c.get("/labs/series?tab=history", headers=ADMIN).data.decode()
    assert "Rexus" in hist and "Undo" in hist
    c.post("/labs/series/undo", headers=ADMIN, data={"id": s.sweep.history()[0]["id"]})
    assert s.live["3"] == []
    assert c.get("/api/labs/series/status", headers=ADMIN).get_json()["running"] is False


def test_shelf_label_writes_land_in_the_same_history(app, s):
    s.index = [book("2", "Regicide")]
    c = app.test_client()
    c.post("/api/library/series", json={"item": "2", "series": CC, "seq": 2}, headers=ADMIN)
    (h,) = s.sweep.history()
    assert (h["user"], h["source"], h["after_text"]) == ("boss", "shelf", f"{CC} #2")
