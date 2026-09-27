"""Capture speed-ups: kept-alive session with polite retries, parallel pools, WebP frames, the
kept-frame setting, the spinner's progressive load order and long-lived cache headers."""
import io
import json
import os
import re
import subprocess

import pytest
import requests
from PIL import Image

import quote_media
import spin_capture as sc
import spin_jobs

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _jpeg(w=1200, h=900):
    b = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(b, "JPEG", exif=Image.Exif())
    return b.getvalue()


class R:
    def __init__(self, status=200, body=b"", ctype="image/jpeg", headers=None):
        self.status_code, self._body = status, body
        self.headers = {"Content-Type": ctype, **(headers or {})}

    def iter_content(self, n):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def no_sleep(monkeypatch):
    waits = []
    monkeypatch.setattr(sc.time, "sleep", waits.append)
    return waits


def seq(monkeypatch, method, answers):
    calls = []

    def fn(self, url, *a, **kw):
        calls.append(url)
        x = answers.pop(0)
        if isinstance(x, Exception):
            raise x
        return x
    monkeypatch.setattr(requests.Session, method, fn)
    return calls


def test_fetch_retries_politely_then_succeeds(monkeypatch, no_sleep):
    calls = seq(monkeypatch, "get", [R(503), R(429, headers={"Retry-After": "3"}), R(200, _jpeg())])
    assert sc._fetch("https://cdn.test/f/1.webp")[:3] == b"\xff\xd8\xff"
    assert len(calls) == 3 and no_sleep == [0.5, 3.0]          # backoff, Retry-After respected (capped at 5 s)


def test_fetch_gives_up_after_retries_and_never_retries_a_404(monkeypatch, no_sleep):
    calls = seq(monkeypatch, "get", [requests.Timeout(), requests.ConnectionError(), requests.Timeout()])
    assert sc._fetch("https://cdn.test/f/1.webp") is None and len(calls) == sc.RETRIES + 1
    calls = seq(monkeypatch, "get", [R(404)])
    assert sc._fetch("https://cdn.test/f/2.webp") is None and len(calls) == 1
    seq(monkeypatch, "get", [R(200, b"<html>not an image</html>", "text/html")])
    assert sc._fetch("https://cdn.test/f/3.webp") is None


def test_upload_retries_and_accepts_its_own_earlier_upload(monkeypatch, no_sleep):
    calls = seq(monkeypatch, "post", [R(502), R(409)])        # the first try landed; the retry sees it
    sc._upload_frame("https://db.test", "k", "a/000.webp", b"x")
    assert len(calls) == 2
    seq(monkeypatch, "post", [R(409)])                        # a 409 on the FIRST try is a real error
    with pytest.raises(RuntimeError):
        sc._upload_frame("https://db.test", "k", "a/000.webp", b"x")


def test_clean_frame_is_webp_720_without_metadata():
    out, size = sc.clean_frame(_jpeg())
    im = Image.open(io.BytesIO(out))
    assert im.format == "WEBP" and size == (720, 540) and im.size == size
    assert not im.info.get("exif") and not im.info.get("icc_profile")


def test_kept_frames_setting():
    try:
        assert sc.MAX_FRAMES == 72
        assert sc.set_max_frames("120") == 120 and sc.pick(240, 37)[0].__len__() == 120
        assert sc.set_max_frames("7") == 120                  # under the 24-frame minimum: ignored
        assert sc.set_max_frames("junk") == 120
    finally:
        sc.MAX_FRAMES = 72
    idx, top = sc.pick(240, 37)
    assert len(idx) == 72 and idx[top] == 37                  # top_index always kept
    idx, top = sc.pick(30, 29)
    assert idx == list(range(30)) and top == 29               # fewer than 72: all kept


def test_first_view_frames():
    fv = sc.first_view(72, 5)
    assert fv[0] == 5 % 8 and 5 in fv and len(fv) == 9 and all((i - 5) % 8 == 0 for i in fv)


def test_frame_ext_by_version():
    assert spin_jobs.frame_ext(5) == "webp" and spin_jobs.frame_ext(4) == "jpg"
    st = {"video_url": "https://video.alldiamondeverything.com/v/abcdefgh23"}
    spin_jobs.apply(st, {"ok": True, "spin": {"id": "c" * 32, "n": 72, "top": 3, "v": 5}})
    assert st["image_url"] == quote_media.MEDIA_BASE + "c" * 32 + "/003.webp"


def test_spinner_load_order():
    """Top frame first, then every 8th frame from it, then the rest; every frame exactly once."""
    js = open(os.path.join(ROOT, "spin360.js")).read()
    body = re.search(r"(function loadOrder\(n\) \{.*?\n  \})", js, re.S).group(1)
    out = subprocess.run(["node", "-e", "var COARSE = 8;" + body + "; process.stdout.write(JSON.stringify("
                          "[loadOrder(72), loadOrder(30)]))"], capture_output=True, text=True, check=True).stdout
    (o72, c72), (o30, c30) = json.loads(out)
    assert sorted(o72) == list(range(72)) and sorted(o30) == list(range(30))
    assert o72[0] == 0 and c72 == 9 and o72[:9] == list(range(0, 72, 8))
    assert c30 == 4 and o30[:4] == [0, 8, 16, 24]
    assert "loading=\"lazy\"" in js and "IntersectionObserver" in js and "'webp' : 'jpg'" in js


def test_get_quote_serves_webp_frame_image():
    frame = quote_media.MEDIA_BASE + "c" * 32 + "/003.webp"
    js = f"""
    const row = {json.dumps({'client': 'Pure Carbon Group', 'expires_at': '2099-01-01T00:00:00Z', 'stones': [
        {'cert_last4': '1', 'video_url': '', 'image_url': frame, 'spin': {'id': 'c' * 32, 'n': 72, 'top': 3, 'v': 5}}]})};
    global.fetch = async () => ({{ ok: true, json: async () => [row] }});
    require('{ROOT}/netlify/functions/get-quote.js').handler({{ path: '/get-quote/abcd1234' }})
      .then(r => process.stdout.write(r.body));
    """
    env = {**os.environ, "SPIN_ENABLED": "test", "SUPABASE_URL": "https://db.test", "SUPABASE_SERVICE_KEY": "k"}
    out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True, env=env).stdout)
    st = out["stones"][0]
    assert st["image_url"] == frame and st["spin"] == {"id": "c" * 32, "n": 72, "top": 3, "v": 5}


def test_long_lived_cache_headers():
    toml = open(os.path.join(ROOT, "netlify.toml")).read()
    assert re.search(r'for = "/media/\*"\s+\[headers.values\]\s+Cache-Control = "public, max-age=31536000, immutable"', toml)
    assert sc.CACHE_CONTROL == "public, max-age=31536000, immutable"
    assert '"public, max-age=31536000, immutable"' in open(os.path.join(ROOT, "diamond_redact_app/quote_media.py")).read()
