"""The whole app, headless (Streamlit AppTest) against a fake database and a fake stone API."""
import json
import re
import subprocess
import os

import pytest
from streamlit.testing.v1 import AppTest

import cert_attach
import fakecerts as F
import quote_media
import stone_source
from fakeapi import HOST, FakeAPI, stone
from fakesb import FakeSupabase

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
REMOVED = ["Revert bad 360", "Remove the promo clip", "List affected stones", "360 upgrade", "Media upgrade",
           "Capture code", "Capture 360 frames", "Find stones with the promo clip"]
SUPPLIER_WORDS = re.compile(r"nivoda|example-supplier", re.I)


def texts(at):
    """Every label / value / body string anywhere in the rendered app."""
    out = []

    def walk(node):
        proto = getattr(node, "proto", None)
        if proto is not None:
            out.append(str(proto))
        for attr in ("label", "value", "body"):
            try:
                v = getattr(node, attr)
                if isinstance(v, str):
                    out.append(v)
            except Exception:
                pass
        for ch in (getattr(node, "children", None) or {}).values():
            walk(ch)
    walk(at._tree)
    return "\n".join(out)


STONES = [
    stone("a1", "LG833689789", "IGI", 150000, lg=True),
    stone("g1", "2141438167", "GIA", 800000, lg=False),
    stone("g2", "2141438167", "GIA", 750000, lg=False, availability="ON_HOLD"),
    stone("s1", "7000000001", "GIA", 500000, lg=False, stock="AB-12345", video=False, image=False, pdf=False),
]
PDFS = {f"{HOST}/certs/LG833689789.pdf": F.igi(), f"{HOST}/certs/2141438167.pdf": F.gia()}


@pytest.fixture
def env(monkeypatch):
    for k, v in {"APP_PASSWORD": "pw", "SUPABASE_SERVICE_KEY": "service", "LS_API_URL": "https://api.test/graphql",
                 "LS_USERNAME": "u", "LS_PASSWORD": "p", "DEFAULT_USD_CAD": "1.37", "DEFAULT_MARKUP_PCT": "20",
                 "SPIN_ENABLED": ""}.items():
        monkeypatch.setenv(k, v)
    for k in ("ANTHROPIC_API_KEY", "NIVODA_API_URL", "NIVODA_USERNAME", "NIVODA_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    sb = FakeSupabase().install(monkeypatch)
    api = FakeAPI(STONES)
    stone_source.Client._schema_cache.clear()
    stone_source.Client._media_off.clear()
    monkeypatch.setattr(stone_source.Client, "_post", lambda self, q, variables=None, token=None: api.post(self, q))

    def fetch(url):
        if url in PDFS:
            return PDFS[url]
        raise cert_attach.Skip("Certificate not attached: download failed (HTTP 404)")
    monkeypatch.setattr(cert_attach, "download", fetch)

    def rehost(sb_url, key, items):      # media copies, without network
        return [({"url": quote_media.MEDIA_BASE + "a" * 32 + ".mp4" if v else "", "how": "hosted" if v else "dropped",
                  "note": ""},
                 {"url": quote_media.MEDIA_BASE + "b" * 32 + ".jpg" if i else "", "how": "hosted" if i else "dropped",
                  "note": ""}) for v, i in items]
    monkeypatch.setattr(quote_media, "process_stones", rehost)
    return sb, api


def boot(authed=True):
    at = AppTest.from_file(APP, default_timeout=60)
    if authed:
        at.session_state["authed"] = True
    at.run()
    return at


def test_login_screen_headless(env):
    at = boot(authed=False)
    assert not at.exception
    assert "Log in" in texts(at)


def test_boots_and_removed_tools_are_gone(env):
    at = boot()
    assert not at.exception, at.exception
    t = texts(at)
    assert "Create quote" in t and "Live Search" in t
    for word in REMOVED:
        assert word not in t, word
    assert "One-time fix: clear links to deleted media files" in t
    assert not SUPPLIER_WORDS.search(t)
    assert "Hide stones with no video/360" in t and "Hide stones with no image" in t


def test_removed_code_is_gone():
    here = os.path.dirname(APP)
    src = "\n".join(open(os.path.join(here, f)).read() for f in ("app.py", "spin_jobs.py", "stone_source.py"))
    for name in ("revert_bad", "find_promo", "clear_promo", "start_upgrade", "upgradable", "def audit",
                 "REPAIR_BUTTONS_ENABLED", "stone_viewer", "STONE_LOOKUP", "PROMO_FILE"):
        assert name not in src, name
    # the kill switch and the dormant capture code stay
    assert 'get_setting("SPIN_ENABLED")' in src and "class SaveJobs" in src
    assert os.path.exists(os.path.join(here, "spin_capture.py"))


def test_criteria_search_media_filter_on_and_off(env):
    sb, api = env
    at = boot()
    at.button(key="ls_search").click().run()
    assert not at.exception, at.exception
    q = [x for x in api.queries if "diamonds_by_query(query:" in x]
    assert any("has_image: true" in x and "has_v360: true" in x for x in q)       # server-side, merged
    assert any("has_image: true" in x and "has_video: true" in x for x in q)
    t = texts(at)
    assert "Media: image" in t and "server-side" in t
    assert "8. Media filter" in t and "9. Certificate files" in t and "certificate.pdfUrl" in t
    n_on = t.count("Add to quote")
    # off: the stone without media comes back
    at.checkbox(key="ls_hide_novid").uncheck().run()
    at.checkbox(key="ls_hide_noimg").uncheck().run()
    assert "The criteria changed since the last search" in texts(at)
    api.queries.clear()
    at.button(key="ls_search").click().run()
    q = [x for x in api.queries if "diamonds_by_query(query:" in x]
    assert q and not any("has_image" in x or "has_v360" in x or "has_video" in x for x in q)
    assert texts(at).count("Add to quote") > n_on
    assert not SUPPLIER_WORDS.search(re.sub(r'href=\\?"[^"]*"|https?://\S+', "", texts(at)))


def test_lookup_then_save_quote(env):
    sb, api = env
    at = boot()
    at.radio(key="ls_mode").set_value("Look up stones").run()
    at.text_area(key="ls_lk_text").input("LG833689789\n2141438167, AB-12345 999999999 2141438167").run()
    assert "3 number(s) to look up" not in texts(at)
    assert "4 number(s) to look up · 1 duplicate(s) removed" in texts(at)
    at.button(key="ls_lk_go").click().run()
    assert not at.exception, at.exception
    t = texts(at)
    assert "Found: 2" in t and "Multiple matches: 1" in t and "Not found: 1" in t
    assert "On hold" in t and "No media" in t
    assert "Copy not-found list" in t and "999999999" in t
    assert "📄 Cert" in t
    # the cheapest listing of each number is pre-selected: a1, g2 (cheaper of g1/g2), s1
    assert set(at.session_state["ls_picked"]) == {"a1", "g2", "s1"}
    at.button(key="ls_add_quote").click().run()
    assert not at.exception, at.exception
    saved = sb.tables["quotes"][-1]
    blob = json.dumps(saved)
    assert not SUPPLIER_WORDS.search(blob)                        # no supplier name/host in saved data
    for k in ("_cert", "_media", "stock_no", "cert_file", "supplierStockId"):
        assert k not in blob
    by4 = {s["cert_last4"]: s for s in saved["stones"]}
    assert by4["9789"]["pdf_url"].startswith("https://quote.alldiamondeverything.com/certs/")
    assert by4["8167"]["pdf_url"].startswith("https://quote.alldiamondeverything.com/certs/")
    assert by4["0001"]["pdf_url"] == ""                           # no certificate file: not attached
    certs_stored = sb.storage["certificates"]
    assert len(certs_stored) == 2 and all(re.fullmatch(r"[a-f0-9]{32}\.pdf", n) for n in certs_stored)
    t = texts(at)
    assert "no certificate file in the stone data" in t and "Certificate log" in t
    # what a client loads: the get-quote function's output
    out = get_quote(saved)
    cl = json.dumps(out)
    assert not SUPPLIER_WORDS.search(cl)
    for full in ("833689789", "2141438167", "7000000001"):
        assert full not in cl
    for pdf in certs_stored.values():                             # and the certificates themselves
        assert b"833689789" not in pdf and b"2141438167" not in pdf


def get_quote(row):
    """Runs netlify/functions/get-quote.js with a fake database returning `row`."""
    root = os.path.dirname(os.path.dirname(APP))
    js = f"""
    process.env.SUPABASE_URL = 'https://db.test'; process.env.SUPABASE_SERVICE_KEY = 'k';
    const row = {json.dumps({k: row[k] for k in ('client', 'stones')} | {'expires_at': '2099-01-01T00:00:00Z'})};
    global.fetch = async () => ({{ ok: true, json: async () => [row] }});
    require('{root}/netlify/functions/get-quote.js').handler({{ path: '/get-quote/abcd1234' }})
      .then(r => process.stdout.write(r.body));
    """
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)


def test_get_quote_drops_foreign_pdf_links():
    out = get_quote({"client": "X", "stones": [
        {"cert_last4": "1", "pdf_url": "https://evil.example.com/x.pdf"},
        {"cert_last4": "2", "pdf_url": "https://quote.alldiamondeverything.com/certs/" + "c" * 32 + ".pdf"},
        {"cert_last4": "3", "pdf_url": "https://srlbevzrkovruyerixdi.supabase.co/storage/v1/object/public/certificates/ab12_1234.pdf"},
        {"cert_last4": "4", "orig_filename": "Live Search · GIA 2141438167", "ls_ref": "abc"}]})
    s = out["stones"]
    assert "pdf_url" not in s[0] and s[1]["pdf_url"].endswith(".pdf") and s[2]["pdf_url"].endswith("ab12_1234.pdf")
    assert "2141438167" not in json.dumps(out) and "ls_ref" not in json.dumps(out)


def test_dead_media_button(env):
    sb, api = env
    from test_dead_media import QUOTES, LIVE_V, LIVE_I, FRAME, PROMO
    sb.tables["quotes"] = [dict(q) for q in QUOTES]
    sb.storage["quote-media"] = {f: b"x" for f in (LIVE_V, LIVE_I, FRAME)}
    at = boot()
    at.button(key="dead_media_go").click().run()
    assert not at.exception, at.exception
    t = texts(at)
    assert "fields cleared: 2" in t and "ERRORS: 0" in t and PROMO in t
    assert sb.tables["quotes"][0]["stones"][0]["video_url"] == ""
