import os
import sys
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
warnings.filterwarnings("ignore", message=".*fitz.*deprecated.*")

import pytest  # noqa: E402

import stone_source  # noqa: E402


@pytest.fixture
def fake_api(monkeypatch):
    """Returns a factory: fake_api(stones, **schema_kw) -> (FakeAPI, Client)."""
    from fakeapi import FakeAPI

    def make(stones, **kw):
        api = FakeAPI(stones, **kw)
        stone_source.Client._schema_cache.clear()
        stone_source.Client._media_off.clear()
        monkeypatch.setattr(stone_source.Client, "_post",
                            lambda self, q, variables=None, token=None: api.post(self, q, variables, token))
        return api, stone_source.Client("https://api.test/graphql", "user", "pw", {})
    return make


@pytest.fixture(autouse=True)
def _fresh_certificate_cache():
    import cert_attach
    cert_attach._dl_cache.clear()
    yield
