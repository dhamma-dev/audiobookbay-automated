from abb.storage import Store
from tests.conftest import make_config


def db_store(tmp_path):
    store = Store(make_config(log_db_path=str(tmp_path / "test.db")))
    store.init()
    return store


def test_record_and_fetch(tmp_path):
    store = db_store(tmp_path)
    store.record_download("alice", "Book A", "http://x/a", "hash1", "ok", route="tor")
    store.record_download("bob", "Book B", "http://x/b", None, "error", "boom", route="direct")

    rows = store.fetch_download_log()
    assert [r["title"] for r in rows] == ["Book B", "Book A"]  # newest first
    assert rows[1]["infohash"] == "hash1" and rows[1]["route"] == "tor"

    assert [r["user"] for r in store.fetch_download_log(user_filter="alice")] == ["alice"]


def test_wanted_upsert_merges_partial_updates(tmp_path):
    store = db_store(tmp_path)
    store.wanted_upsert({"hc_id": 1, "title": "Dune", "author": "Frank Herbert",
                         "status": "wanted"})
    store.wanted_upsert({"hc_id": 1, "status": "found", "best_title": "Dune M4B"})

    (row,) = store.wanted_rows()
    assert row["title"] == "Dune"            # untouched fields survive the merge
    assert row["status"] == "found" and row["best_title"] == "Dune M4B"


def test_wanted_delete_missing(tmp_path):
    store = db_store(tmp_path)
    for i in (1, 2, 3):
        store.wanted_upsert({"hc_id": i, "title": f"B{i}"})
    store.wanted_delete_missing({1, 3})
    assert sorted(r["hc_id"] for r in store.wanted_rows()) == [1, 3]


def test_memory_fallback_when_log_disabled():
    store = Store(make_config(log_db_path=""))
    store.init()
    store.record_download("alice", "Book", "l", None, "ok")  # silently dropped
    assert store.fetch_download_log() == []

    store.wanted_upsert({"hc_id": 7, "title": "Kept in memory", "status": "wanted"})
    store.wanted_upsert({"hc_id": 7, "status": "found"})
    (row,) = store.wanted_rows()
    assert row["title"] == "Kept in memory" and row["status"] == "found"
    store.wanted_delete_missing(set())
    assert store.wanted_rows() == []


def test_schema_migration_from_v1(tmp_path):
    """A v1 database (without the late-added columns) self-migrates on init."""
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE downloads (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, user TEXT NOT NULL,
        title TEXT NOT NULL, link TEXT, infohash TEXT, client TEXT, route TEXT,
        status TEXT NOT NULL, detail TEXT)""")
    conn.execute("INSERT INTO downloads (ts, user, title, status) VALUES ('t','u','b','ok')")
    conn.commit()
    conn.close()

    store = Store(make_config(log_db_path=str(path)))
    store.init()
    (row,) = store.fetch_download_log()
    assert row["batch_id"] is None  # column added by the migration
