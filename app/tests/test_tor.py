"""Tor routing fails closed: when a request's route is Tor and Tor isn't up
(booting, never started, crashed), AudioBook Bay requests wait or refuse —
they are never quietly sent Direct, which would show the mirror the server's
real IP. Direct is only ever an explicit choice."""

import threading
import time

import pytest

from abb import create_app
from abb import tor as tor_module
from abb.outbound import Outbound, TorUnavailable
from abb.tor import RESTART_BACKOFF_MAX, TorManager
from tests.conftest import make_config


class StubTor:
    """Just enough TorManager for Outbound/WantedService."""

    def __init__(self, status):
        self._status = status
        self.on_ready = None
        self.renewable = False

    def status(self):
        return self._status


# --- Outbound -------------------------------------------------------------------
def test_tor_route_never_falls_back_to_direct():
    for status in ("unavailable", "starting"):
        ob = Outbound(make_config(use_tor=True), StubTor(status))
        assert ob.route_mode() == "tor"          # intent survives Tor being down...
        with pytest.raises(TorUnavailable) as err:
            ob.scrape_session()                   # ...and refuses instead of going direct
        assert err.value.tor_status == status


def test_route_sessions_are_labelled_for_the_log():
    tor = StubTor("ready")
    ob = Outbound(make_config(use_tor=True), tor)
    tor.on_ready()                                # Tor bootstrapped -> session built
    assert ob.scrape_session() is ob.tor_session
    assert ob.route_of(ob.tor_session) == "tor"

    direct = Outbound(make_config(use_tor=False), StubTor("unavailable"))
    assert direct.scrape_session() is direct.direct_session   # an explicit choice
    assert direct.route_of(direct.direct_session) == "direct"


# --- TorManager lifecycle ---------------------------------------------------------
class RunningProc:
    def poll(self):
        return None


class ExitedProc:
    """A tor process whose output has ended — it exited."""

    def __init__(self, lines, code=1):
        self.stdout = iter(lines)
        self.returncode = None
        self._code = code

    def wait(self, timeout=None):
        self.returncode = self._code
        return self._code

    def poll(self):
        return self.returncode


def test_slow_bootstrap_still_becomes_ready():
    """Past TOR_BOOTSTRAP_TIMEOUT Tor reports unavailable, but a late 100%
    still flips it to ready (it used to be ignored until a restart)."""
    tm = TorManager(make_config(tor_bootstrap_timeout=0.05))
    readied = []
    tm.on_ready = lambda: readied.append(True)
    proc = RunningProc()
    tm._process, tm.starting = proc, True
    ready = threading.Event()
    waiter = threading.Thread(target=tm._await_bootstrap, args=(proc, ready))
    waiter.start()
    deadline = time.monotonic() + 2
    while tm.starting and time.monotonic() < deadline:
        time.sleep(0.01)
    assert tm.status() == "unavailable"           # timed out: says so...
    ready.set()
    waiter.join(2)
    assert tm.status() == "ready" and readied == [True]   # ...then recovers


def test_tor_exit_flips_unavailable_and_relaunches():
    tm = TorManager(make_config())
    scheduled = []
    tm._schedule_restart = lambda: scheduled.append(True)
    proc = ExitedProc(["Jul 01 [notice] Bootstrapped 100% (done): Done"])
    tm._process, tm.available, tm.managed = proc, True, True
    tm._consume_output(proc, threading.Event())
    assert tm.status() == "unavailable" and not tm.renewable
    assert scheduled == [True]


def test_exit_during_shutdown_is_not_a_crash():
    tm = TorManager(make_config())
    tm._schedule_restart = lambda: pytest.fail("no relaunch while stopping")
    proc = ExitedProc([], code=0)
    tm._process, tm._stopping = proc, True
    tm._consume_output(proc, threading.Event())


def test_launch_failure_is_logged_and_retried_not_raised(monkeypatch):
    def boom(*a, **k):
        raise OSError("tor: permission denied")
    monkeypatch.setattr(tor_module.subprocess, "Popen", boom)
    tm = TorManager(make_config())
    tm._tor_bin = "tor"
    retried = []
    tm._schedule_restart = lambda: retried.append(True)
    tm._launch()                                  # no exception escapes
    assert tm.status() == "unavailable" and retried == [True]
    tm.stop()                                     # cleans the launch's temp dir


def test_relaunch_backoff_doubles_and_caps(monkeypatch):
    delays = []

    class FakeTimer:
        def __init__(self, delay, fn):
            delays.append(delay)
            self.daemon = False

        def start(self):
            pass

    monkeypatch.setattr(tor_module.threading, "Timer", FakeTimer)
    tm = TorManager(make_config())
    tm._tor_bin = "tor"
    for _ in range(7):
        tm._schedule_restart()
    assert delays == [10, 20, 40, 80, 160, RESTART_BACKOFF_MAX, RESTART_BACKOFF_MAX]


# --- Routes -------------------------------------------------------------------------
class FakeResp:
    status_code = 200
    text = "<html></html>"   # a results page with no posts


def _tor_app(tmp_path, **overrides):
    """USE_TOR=true, but Tor never starts (start=False) -> 'unavailable'.
    Every Direct request is recorded, so tests can prove none happened."""
    cfg = make_config(use_tor=True, log_db_path=str(tmp_path / "t.db"),
                      download_client="qbittorrent", dl_host="h", dl_port="1",
                      dl_username="u", dl_password="p", **overrides)
    app = create_app(cfg, start=False)
    app.config.update(TESTING=True)
    svc = app.extensions["abb"]
    svc.store.init()
    direct_calls = []
    svc.outbound.direct_session.get = lambda *a, **k: direct_calls.append(a) or FakeResp()
    return app, svc, direct_calls


def _token(client):
    page = client.get("/").data
    return page.split(b'name="csrf-token" content="')[1].split(b'"')[0].decode()


def test_search_waits_for_tor_instead_of_going_direct(tmp_path):
    app, _svc, direct_calls = _tor_app(tmp_path)
    c = app.test_client()
    page = c.get("/?q=dune").data.decode()
    assert "Tor · down" in page and 'id="tor-booting"' in page
    assert 'value="dune"' in page and "results for" not in page   # kept, not searched

    r = c.post("/", data={"query": "dune", "csrf_token": _token(c)})
    assert r.status_code == 503 and "paused" in r.get_json()["message"]
    assert direct_calls == []


def test_send_waits_for_tor_instead_of_going_direct(tmp_path):
    app, svc, direct_calls = _tor_app(tmp_path)
    c = app.test_client()
    link = "https://audiobookbay.lu/abss/dune/"
    r = c.post("/send", json={"link": link, "title": "Dune"})
    assert r.status_code == 503 and "Tor" in r.get_json()["message"]
    (entry,) = svc.store.fetch_download_log()
    assert entry["status"] == "error" and entry["route"] == "tor"   # logged truthfully

    r = c.post("/send/batch", json={"items": [{"link": link, "title": "Dune"}]})
    assert r.status_code == 503
    assert direct_calls == []


def test_quick_add_waits_for_tor_without_a_retry_backoff(tmp_path):
    app, svc, direct_calls = _tor_app(tmp_path, hardcover_api_key="k")
    c = app.test_client()
    r = c.post("/wanted/add", data={"csrf_token": _token(c), "title": "Dune",
                                    "author": "Frank Herbert"}, follow_redirects=True)
    assert "waiting for Tor" in r.data.decode()
    (row,) = svc.store.wanted_rows()
    # Nothing went out, so it's not a failed search: still due the moment
    # Tor is back, not after the retry backoff.
    assert row["status"] == "wanted" and row["searched_at"] is None
    assert direct_calls == []


def test_choosing_direct_still_works_when_tor_is_down(tmp_path):
    app, _svc, direct_calls = _tor_app(tmp_path)
    c = app.test_client()
    token = _token(c)
    assert c.post("/settings/route", json={"mode": "direct"}).status_code == 200
    r = c.post("/", data={"query": "dune", "csrf_token": token})
    assert r.status_code == 200 and len(direct_calls) >= 1   # the user's explicit choice
    assert '<span class="conn-mode-label">Direct</span>' in c.get("/").data.decode()
