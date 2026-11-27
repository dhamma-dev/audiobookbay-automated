"""Series gap fill: the targeted searches for books a shelf is missing, with
the scraper, library and AI verdict stubbed — no network."""

import pytest

from abb import gapfill
from abb.outbound import TorUnavailable

AUTHOR = "Dakota Krout"
LABEL = "The Completionist Chronicles"


def listing(title, slug, fmt="M4B", kbps="64 Kbps"):
    return {"title": title, "link": f"https://audiobookbay.lu/abss/{slug}/",
            "cover": f"https://audiobookbay.lu/c/{slug}.jpg", "size": "500 MB",
            "format": fmt, "bitrate": kbps, "language": "English", "keywords": [],
            "is_m4b": fmt == "M4B"}


@pytest.fixture
def svc(app):
    s = app.extensions["abb"]
    s.searches = []
    s.verdicts = []
    s.rank.wanted_verdict = lambda title, author, listings: (
        s.verdicts.append((title, [x["title"] for x in listings])) or None)
    return s


def stub_search(s, by_query):
    def search(q, max_pages=5, sess=None):
        s.searches.append(q)
        hit = by_query.get(q, [])
        return None if hit is None else [dict(b) for b in hit]
    s.scraper.search = search


def test_clean_gaps_validates_and_caps():
    raw = [{"seq": 2, "title": " Regicide "}, {"seq": "3.0", "title": "Rexus"},
           {"seq": "x", "title": "bad"}, {"seq": 4}, "junk", {"seq": True, "title": "t"}]
    assert gapfill.clean_gaps(raw) == [{"seq": "2", "title": "Regicide"},
                                       {"seq": "3", "title": "Rexus"}]
    assert gapfill.clean_gaps(None) == []
    many = [{"seq": i, "title": f"Book {i}"} for i in range(1, 30)]
    assert len(gapfill.clean_gaps(many)) == gapfill.MAX_GAPS


def test_title_search_finds_the_book_and_refuses_siblings(svc):
    stub_search(svc, {"regicide": [
        listing("Rexus: The Completionist Chronicles, Book 3 - Dakota Krout", "rexus"),
        listing("Regicide - Some Other Author", "other"),
        listing("Regicide - Dakota Krout", "mp3", fmt="MP3", kbps="128 Kbps"),
        listing("Regicide (The Completionist Chronicles Book 2) - Dakota Krout", "m4b"),
        listing("(REQ) Regicide - Dakota Krout", "req"),
    ]})
    (r,) = gapfill.fill_by_title(svc, LABEL, AUTHOR, [{"seq": "2", "title": "Regicide"}])
    assert r["status"] == "found"
    # M4B first; the sibling volume, the other author and the request are out.
    assert [b["link"].split("/")[-2] for b in r["books"]] == ["m4b", "mp3"]
    assert svc.searches == ["regicide"]      # found on the first rung: no more searches
    assert svc.verdicts == []                # the matcher settled it: no AI call


def test_title_ladder_then_ai_verdict_for_badly_labelled_uploads(svc):
    odd = listing("Completionist Chronicles 2 (Unabridged)", "odd")
    stub_search(svc, {"regicide": [], "regicide krout": [odd]})
    svc.rank.wanted_verdict = lambda title, author, listings: {
        "match_found": True, "ranked": [0, 7, 0], "reason": "book 2",
        "notes": [{"id": 0, "note": "untitled upload"}]}
    (r,) = gapfill.fill_by_title(svc, LABEL, AUTHOR, [{"seq": "2", "title": "Regicide"}])
    assert svc.searches == ["regicide", "regicide krout"]
    assert r["status"] == "found"
    assert [(b["link"], b["note"]) for b in r["books"]] == [(odd["link"], "untitled upload")]


def test_missing_and_unreachable_are_different(svc):
    stub_search(svc, {"regicide": [listing("Unrelated Book - Someone", "x")],
                      "rexus": None})
    out = gapfill.fill_by_title(svc, LABEL, AUTHOR, [
        {"seq": "2", "title": "Regicide"}, {"seq": "3", "title": "Rexus"},
        {"seq": "4", "title": "Raze"}])
    # The run stops at the first unreachable search: Raze isn't attempted.
    assert [(r["seq"], r["status"]) for r in out] == [("2", "missing"), ("3", "unreachable")]


def test_owned_gap_is_never_searched(svc, monkeypatch):
    svc.library.enabled = True
    index = [{"title": "Regicide", "author": AUTHOR, "subtitle": "", "language": "",
              "series": [(LABEL, "2")], "asin": "", "isbn": ""}]
    monkeypatch.setattr(svc.library, "get_index", lambda max_age=None: index)
    stub_search(svc, {})
    (r,) = gapfill.fill_by_title(svc, "Completionist Chronicles", AUTHOR,
                                 [{"seq": "2", "title": "Regicide"}])
    assert r["status"] == "owned" and svc.searches == []


def test_author_sweep_is_one_search_for_every_gap(svc):
    stub_search(svc, {"dakota krout": [
        listing("Rexus - Dakota Krout", "rexus"),
        listing("Dungeon Born - Dakota Krout", "dungeon"),
        listing("Completionist Chronicles 4 - Dakota Krout", "four"),
    ]})
    seen = []

    def verdict(title, author, listings):
        seen.append((title, [x["title"] for x in listings]))
        return {"match_found": True, "ranked": [0], "notes": [], "reason": ""}
    svc.rank.wanted_verdict = verdict
    out = gapfill.fill_by_author(svc, LABEL, "Dakota Krout, Luke Daniels", [
        {"seq": "3", "title": "Rexus"}, {"seq": "4", "title": "Raze"},
        {"seq": "5", "title": "Ra"}])
    assert svc.searches == ["dakota krout"]
    assert [(r["seq"], r["status"]) for r in out] == [
        ("3", "found"), ("4", "found"), ("5", "missing")]
    # Only the plausibly-this-book listing reached the AI, and only for the
    # gap the matcher couldn't settle (series name + its number, no title).
    assert seen == [(f"Raze ({LABEL}, Book 4)", ["Completionist Chronicles 4 - Dakota Krout"])]


def test_endpoint_returns_cards_numbered_past_the_page(client):
    s = client.application.extensions["abb"]
    stub_search(s, {"regicide": [listing("Regicide - Dakota Krout", "reg")]})
    s.searches = []
    r = client.post("/api/series/fill", json={
        "label": LABEL, "author": AUTHOR, "next_id": 40,
        "gaps": [{"seq": 2, "title": "Regicide"}]})
    assert r.status_code == 200
    (gap,) = r.get_json()["gaps"]
    assert gap["seq"] == "2" and gap["status"] == "found" and "books" not in gap
    (card,) = gap["cards"]
    assert card["id"] == 40 and 'data-result-id="40"' in card["html"]
    assert 'data-link="https://audiobookbay.lu/abss/reg/"' in card["html"]

    assert client.post("/api/series/fill", json={"label": LABEL, "gaps": []}).status_code == 400


def test_endpoint_fails_closed_without_tor(client):
    s = client.application.extensions["abb"]

    def search(q, max_pages=5, sess=None):
        raise TorUnavailable("Tor isn't ready")
    s.scraper.search = search
    r = client.post("/api/series/fill", json={
        "label": LABEL, "author": AUTHOR, "gaps": [{"seq": 2, "title": "Regicide"}]})
    assert r.status_code == 503 and "Tor" in r.get_json()["message"]


def test_wanted_add_endpoint(tmp_path):
    from abb import create_app
    from tests.conftest import make_config
    app = create_app(make_config(log_db_path=str(tmp_path / "d.db")), start=False)
    s = app.extensions["abb"]
    s.store.init()
    c = app.test_client()
    assert c.post("/api/wanted/add", json={"title": "Raze", "author": AUTHOR}
                  ).get_json() == {"outcome": "added"}
    assert c.post("/api/wanted/add", json={"title": "Raze", "author": AUTHOR}
                  ).get_json() == {"outcome": "duplicate"}
    (row,) = s.store.wanted_rows()
    assert row["title"] == "Raze" and row["hc_id"] < 0 and not row.get("searched_at")
    assert c.post("/api/wanted/add", json={"title": " "}).status_code == 400
