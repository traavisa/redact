"""The stone source unreachable or erroring: one clear message, details kept for the server log."""
import json

import pytest
import requests

import stone_source
from stone_source import UNAVAILABLE, Client, SourceError, SourceUnavailable
from test_app import boot, env, texts  # noqa: F401  (env is a fixture)

REAL_POST = Client._post           # before any fixture replaces it with the fake API


class R:
    def __init__(self, status=200, body=None, text=None):
        self.status_code, self._body = status, body

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


def client_with(monkeypatch, answers):
    """A real Client whose HTTP calls return (or raise) `answers` in order."""
    calls = []

    def post(url, **kw):
        calls.append(json.loads(kw["data"])["query"][:40])
        a = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(Client, "_post", REAL_POST)
    monkeypatch.setattr(stone_source.requests, "post", post)
    monkeypatch.setattr(Client, "schema", lambda self: {"verified": False, "filters": {}, "media": {}})
    return Client("https://api.test/graphql", "user", "secretpw", {}), calls


TOKEN = R(200, {"data": {"authenticate": {"username_and_password": {"token": "t"}}}})
OK = R(200, {"data": {"diamonds_by_query": {"total_count": 0, "items": []}}})


@pytest.mark.parametrize("answers, cause", [
    ([requests.Timeout(), requests.Timeout()], "Timed out"),                        # repeated timeouts at sign-in
    ([TOKEN, requests.Timeout(), requests.Timeout()], "Timed out"),                 # repeated timeouts on the search
    ([R(503, {})], "HTTP 503"),
    ([TOKEN, R(502, {})], "HTTP 502"),
    ([requests.ConnectionError()], "Connection error"),
    ([R(200, None)], "not JSON"),
    ([R(200, {"data": {"authenticate": {"username_and_password": None}}})], None),  # sign-in fails
    ([R(401, {})], "HTTP 401"),                                                     # sign-in refused
])
def test_unavailable_message(monkeypatch, answers, cause):
    c, _ = client_with(monkeypatch, answers)
    with pytest.raises(SourceUnavailable) as e:
        c.search({"labgrown": True}, 10)
    assert str(e.value) == UNAVAILABLE == "The stone source isn't responding right now. Try again in a few minutes."
    if cause:
        assert any(cause in x for x in c.diag["api_errors"]), c.diag["api_errors"]
    assert "secretpw" not in json.dumps(c.diag)


def test_one_timeout_is_retried(monkeypatch):
    c, calls = client_with(monkeypatch, [TOKEN, requests.Timeout(), OK])
    assert c.search({"labgrown": True}, 10) == ([], 0)
    assert c.diag["timeouts"] == 1


def test_rejected_query_keeps_its_own_message(monkeypatch):
    c, _ = client_with(monkeypatch, [TOKEN, R(200, {"errors": [{"message": "bad field"}]})])
    with pytest.raises(SourceError) as e:
        c.search({"labgrown": True}, 10)
    assert not isinstance(e.value, SourceUnavailable) and "rejected" in str(e.value)


def test_app_shows_the_message_for_search_and_lookup(env, monkeypatch, capfd):
    monkeypatch.setattr(Client, "_post", REAL_POST)          # real client; the network answers 503
    monkeypatch.setattr(stone_source.requests, "post", lambda url, **kw: R(503, {}))
    stone_source.Client._schema_cache.clear()
    at = boot()
    at.button(key="ls_search").click().run()
    assert not at.exception, at.exception
    assert UNAVAILABLE in texts(at) and "timed out" not in texts(at)
    at.radio(key="ls_mode").set_value("Look up stones").run()
    at.text_area(key="ls_lk_text").input("LG833689789").run()
    at.button(key="ls_lk_go").click().run()
    assert not at.exception, at.exception
    assert UNAVAILABLE in texts(at)
    out = capfd.readouterr().out
    assert "[live-search] " in out and "[live-search-lookup] " in out and "HTTP 503" in out
