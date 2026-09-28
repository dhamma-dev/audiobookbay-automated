"""Gemini calls: every one is bounded by GEMINI_TIMEOUT (the SDK's default is
no timeout at all), and only a model that rejects the thinking config loses
the fast thinking=0 path. A timeout, a quota error or an overloaded model
must not — each used to switch it off for good."""

import httpx
import pytest
from google import genai
from google.genai import errors

from abb.smart_sort import RankService
from tests.conftest import make_config

RANKING = ('{"ordering": [0], "buckets": [], "ambiguous": false, '
           '"interpretations": [], "series": [], "editions": []}')
RESULTS = [{"id": 0, "title": "Dune - Frank Herbert"}]


class FakeResponse:
    def __init__(self, text):
        self.text = text


def fake_client(monkeypatch, *outcomes):
    """Stand in for genai.Client: each generate_content call takes the next
    outcome (an exception to raise, or response text). Records the client's
    http_options and each call's thinking config."""
    seen = {"http_options": [], "thinking": []}
    queue = list(outcomes)

    class Models:
        def generate_content(self, model, contents, config):
            seen["thinking"].append(config.thinking_config)
            outcome = queue.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return FakeResponse(outcome)

    class Client:
        def __init__(self, api_key=None, http_options=None):
            seen["http_options"].append(http_options)
            self.models = Models()

    monkeypatch.setattr(genai, "Client", Client)
    return seen


def service(**overrides):
    return RankService(make_config(gemini_api_key="k", **overrides))


def api_error(cls, code, status, message):
    """A real google-genai error, shaped like the API's JSON error body."""
    return cls(code, {"error": {"code": code, "status": status, "message": message}})


def test_every_call_is_bounded_by_the_timeout(monkeypatch):
    seen = fake_client(monkeypatch, RANKING)
    service(gemini_timeout=60.0).rank("dune", RESULTS)
    assert seen["http_options"][0].timeout == 60000   # the SDK takes milliseconds

    seen = fake_client(monkeypatch, RANKING)
    service(gemini_timeout=None).rank("dune", RESULTS)
    assert seen["http_options"][0] is None            # GEMINI_TIMEOUT=off: unbounded


def test_a_timeout_is_one_clean_failure_that_keeps_the_fast_path(monkeypatch):
    seen = fake_client(monkeypatch, httpx.ReadTimeout("timed out"))
    rs = service(gemini_timeout=60.0)
    with pytest.raises(TimeoutError, match="within 60s"):
        rs.rank("dune", RESULTS)
    assert len(seen["thinking"]) == 1   # not retried: it would only time out again
    assert rs._thinking_supported       # and the fast path survives


@pytest.mark.parametrize("error", [
    api_error(errors.ClientError, 429, "RESOURCE_EXHAUSTED", "Quota exceeded for quota metric"),
    api_error(errors.ServerError, 503, "UNAVAILABLE", "The model is overloaded."),
], ids=["quota", "overloaded"])
def test_transient_errors_keep_the_fast_path(monkeypatch, error):
    seen = fake_client(monkeypatch, error)
    rs = service()
    with pytest.raises(errors.APIError):
        rs.rank("dune", RESULTS)
    assert len(seen["thinking"]) == 1 and rs._thinking_supported


def test_a_model_rejecting_thinking_retries_once_without_it(monkeypatch):
    rejection = api_error(errors.ClientError, 400, "INVALID_ARGUMENT",
                          "Thinking budget is not supported for this model.")
    seen = fake_client(monkeypatch, rejection, RANKING)
    rs = service()
    assert rs.rank("dune", RESULTS)["ordering"] == [0]
    assert seen["thinking"][0] is not None and seen["thinking"][1] is None
    assert not rs._thinking_supported   # never sent to this model again


def test_a_verdict_timeout_falls_back_instead_of_stalling_the_worker(monkeypatch):
    fake_client(monkeypatch, httpx.ReadTimeout("timed out"))
    verdict = service().wanted_verdict("Dune", "Frank Herbert", RESULTS)
    assert verdict is None   # -> the deterministic pick; the worker moves on
