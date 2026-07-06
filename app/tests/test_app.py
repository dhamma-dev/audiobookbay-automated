"""Route smoke tests through the Flask test client — the whole app wired
(start=False: no Tor, no DB, no worker), features degrading exactly as
their flags say."""

import json

from abb import create_app
from tests.conftest import make_config


def test_pages_render_with_everything_disabled(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"Find your next listen" in r.data
    # The config-error banner shows when DOWNLOAD_CLIENT is unset.
    assert b"No download client configured" in r.data

    assert client.get("/status").status_code == 200      # renders the error state
    assert client.get("/upgrades").status_code == 200    # "not connected" empty state
    assert client.get("/wanted").status_code == 200
    assert client.get("/log").status_code == 200


def test_security_headers_and_no_third_party_scripts(client):
    r = client.get("/")
    assert "Content-Security-Policy" in r.headers
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"
    assert b"unpkg.com" not in r.data                    # icons are vendored


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"


def test_send_rejects_non_abb_links(client):
    r = client.post("/send", json={"link": "https://evil.example/x", "title": "T"})
    assert r.status_code == 400
    assert "AudiobookBay" in r.get_json()["message"]


def test_send_reports_missing_client_config(client):
    r = client.post("/send", json={"link": "https://audiobookbay.lu/abss/x/", "title": "T"})
    assert r.status_code == 503
    assert "DOWNLOAD_CLIENT" in r.get_json()["message"]


def test_csrf_blocks_form_posts_without_token(client):
    r = client.post("/wanted/sync", data={"anything": "1"})
    assert r.status_code == 403
    assert "CSRF" in r.get_json()["message"]


def test_csrf_accepts_form_posts_with_token(client):
    # Render a page first so the session mints a token, then echo it back.
    page = client.get("/")
    token = page.data.split(b'name="csrf-token" content="')[1].split(b'"')[0].decode()
    r = client.post("/wanted/sync", data={"csrf_token": token})
    assert r.status_code == 503  # past CSRF; rejected because Hardcover is off
    assert "Hardcover" in r.get_json()["message"]


def test_route_setting(client):
    r = client.post("/settings/route", json={"mode": "direct"})
    assert r.status_code == 200 and r.get_json()["mode"] == "direct"
    # Tor was never started in tests -> switching to it is refused.
    assert client.post("/settings/route", json={"mode": "tor"}).status_code == 409
    assert client.post("/settings/route", json={"mode": "carrier-pigeon"}).status_code == 400


def test_api_rank_disabled_without_key(client):
    r = client.post("/api/rank", json={"query": "q", "results": [{"id": 0, "title": "x"}]})
    assert r.status_code == 503


def test_api_ownership_disabled_returns_empty(client):
    r = client.post("/api/ownership", json={"items": [{"id": 0, "title": "x"}]})
    assert r.status_code == 200 and r.get_json() == {"ownership": []}


def test_api_connection_shape(client):
    d = client.get("/api/connection").get_json()
    assert d["tor_status"] == "unavailable" and d["route_mode"] == "direct"
    assert d["tor_available"] is False and d["tor_renewable"] is False


def test_covers_proxy_disabled_and_allowlisted():
    app = create_app(make_config(cover_proxy=True), start=False)
    app.config.update(TESTING=True)
    c = app.test_client()
    # Non-ABB hosts are refused — this must never be an open proxy.
    assert c.get("/covers?u=https://evil.example/a.jpg").status_code == 403

    plain = create_app(make_config(), start=False)
    plain.config.update(TESTING=True)
    assert plain.test_client().get("/covers?u=https://audiobookbay.lu/x.jpg").status_code == 404


def test_search_get_deeplink_renders_unsearched_without_scrape(client):
    # No Tor and USE_TOR=false -> direct mode; but we don't want a real scrape
    # in tests, so just confirm the empty GET renders the search page shell.
    r = client.get("/")
    assert b"search-form" in r.data


def test_putio_oauth_requires_putio_client(client):
    assert client.get("/putio/auth").status_code == 400
    assert client.get("/putio/callback").status_code == 400


def test_wanted_page_renders_rows(tmp_path):
    """The enabled dashboard path: seeded rows, alternates tray, action forms —
    catches template/endpoint regressions the disabled path skips."""
    cfg = make_config(log_db_path=str(tmp_path / "w.db"), hardcover_api_key="k")
    app = create_app(cfg, start=False)
    app.config.update(TESTING=True)
    store = app.extensions["abb"].store
    store.init()
    store.wanted_upsert({
        "hc_id": 1, "title": "Dune", "author": "Frank Herbert", "slug": "dune",
        "status": "found", "best_link": "https://audiobookbay.lu/abss/dune/",
        "best_title": "Dune - Frank Herbert", "best_meta": "M4B · 128 Kbps",
        "verdict": "Exact match; M4B preferred.",
        "candidates": json.dumps([
            {"title": "Dune - Frank Herbert", "link": "https://audiobookbay.lu/abss/dune/",
             "format": "M4B", "is_m4b": True},
            {"title": "Dune (abridged)", "link": "https://audiobookbay.lu/abss/dune2/",
             "format": "MP3", "note": "abridged", "is_m4b": False},
        ])})
    store.wanted_upsert({"hc_id": 2, "title": "Elantris", "author": "Brandon Sanderson",
                         "status": "unmatched", "detail": "no confident match"})

    r = app.test_client().get("/wanted")
    assert r.status_code == 200
    assert b"Dune - Frank Herbert" in r.data
    assert b"Exact match; M4B preferred." in r.data     # the AI verdict line
    assert b"+1 alternative" in r.data                  # the tray toggle
    assert b"/wanted/research/1" in r.data              # per-row re-check form


def test_log_page_renders_entries(tmp_path):
    cfg = make_config(log_db_path=str(tmp_path / "l.db"),
                      log_admin_users=frozenset({"admin"}))
    app = create_app(cfg, start=False)
    app.config.update(TESTING=True)
    store = app.extensions["abb"].store
    store.init()
    store.record_download("alice", "Some Book", "https://audiobookbay.lu/abss/x/",
                          "h", "ok", route="tor")

    c = app.test_client()
    # Admin sees everyone (incl. the Who column with a filter link).
    r = c.get("/log", headers={"X-authentik-username": "admin"})
    assert r.status_code == 200 and b"alice" in r.data and b"Some Book" in r.data
    # Non-admins are locked to their own rows regardless of ?user=.
    r = c.get("/log?user=alice", headers={"X-authentik-username": "mallory"})
    assert r.status_code == 200 and b"Some Book" not in r.data


def test_search_post_renders_result_cards(client):
    """The core flow with the scraper stubbed: cards, M4B ribbon/sort, stable
    ids, and the slim rank payload all come out of a POSTed search."""
    fake = [
        {"title": "Plain MP3 - Author", "link": "https://audiobookbay.lu/abss/a/",
         "cover": "https://audiobookbay.lu/c/a.jpg", "size": "500 MB", "format": "MP3",
         "bitrate": "64 Kbps", "language": "English", "keywords": ["fantasy"],
         "is_m4b": False},
        {"title": "Nice M4B - Author", "link": "https://audiobookbay.lu/abss/b/",
         "cover": "https://audiobookbay.lu/c/b.jpg", "size": "1 GB", "format": "M4B",
         "bitrate": "128 Kbps", "language": "English", "keywords": [], "is_m4b": True},
    ]
    svc = client.application.extensions["abb"]
    svc.scraper.search = lambda q, max_pages=5, sess=None: [dict(b) for b in fake]

    page = client.get("/")
    token = page.data.split(b'name="csrf-token" content="')[1].split(b'"')[0].decode()
    r = client.post("/", data={"query": "anything", "csrf_token": token})
    assert r.status_code == 200
    body = r.data.decode()
    assert "2 results" in body and "1 M4B pick" in body
    # M4B floats above the MP3 upload in the default order.
    assert body.index("Nice M4B") < body.index("Plain MP3")
    assert 'data-result-id="0"' in body and 'data-result-id="1"' in body
    assert 'id="search-results-data"' not in body  # no Gemini key -> no payload blob


def test_wanted_page_shelves_and_skip_flow(tmp_path):
    """Owned and skipped rows leave the active table for their own collapsed
    shelves; skip/unskip round-trips through the real endpoints."""
    cfg = make_config(log_db_path=str(tmp_path / "w.db"), hardcover_api_key="k")
    app = create_app(cfg, start=False)
    app.config.update(TESTING=True)
    store = app.extensions["abb"].store
    store.init()
    store.wanted_upsert({"hc_id": 1, "title": "Queued Book", "author": "A",
                         "status": "wanted"})
    store.wanted_upsert({"hc_id": 2, "title": "Owned Book", "author": "B",
                         "status": "owned"})

    c = app.test_client()
    page = c.get("/wanted").data.decode()
    assert "1 in the pipeline" in page and "1 in library" in page
    assert 'id="wanted-owned-list"' in page          # done shelf, collapsed
    assert "/wanted/skip/1" in page                  # skip on the queued row
    assert "/wanted/skip/2" not in page              # no actions on done rows
    assert "/wanted/research/2" not in page

    token = page.split('name="csrf-token" content="')[1].split('"')[0]
    r = c.post("/wanted/skip/1", data={"csrf_token": token})
    assert r.status_code == 302
    page = c.get("/wanted").data.decode()
    assert "0 in the pipeline" in page and "1 skipped" in page
    assert 'id="wanted-skipped-list"' in page and "/wanted/unskip/1" in page

    r = c.post("/wanted/unskip/1", data={"csrf_token": token})
    assert r.status_code == 302
    assert "1 in the pipeline" in c.get("/wanted").data.decode()

    # Guard surfaces as a conflict, not a silent success.
    assert c.post("/wanted/skip/2", data={"csrf_token": token}).status_code == 409
