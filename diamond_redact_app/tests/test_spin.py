"""360 capture test mode (SPIN_ENABLED=test): capture only for Pure Carbon Group quotes, from the
certificate's 360 API fields only; pages show only captures made by the current code."""
import io
import json
import os
import re
import subprocess
import time

import pytest
from PIL import Image

import quote_media
import spin_capture
import spin_jobs
import stone_source
from fakeapi import HOST, schema, stone
from test_app import boot, env, texts  # noqa: F401  (env is a fixture)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FOLDER = f"{HOST}/v360/frames/abc123"
V360 = {"url": FOLDER, "frame_count": 240, "top_index": 37}
VIEWER = quote_media.VIEWER_LINK_BASE + "abcdefgh23"


def _jpeg():
    b = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 10, 10)).save(b, "JPEG")
    return b.getvalue()


JPEG = _jpeg()


@pytest.fixture
def spin_env(env, monkeypatch):
    """The app with SPIN_ENABLED=test, a stone API whose certificates carry v360 fields, and
    frame servers faked. Records every frame URL read and every non-database request."""
    sb, api = env
    monkeypatch.setenv("SPIN_ENABLED", "test")
    api.schema = schema(v360=True)
    api.stones = [stone("p1", "LG600000001", "IGI", 150000, v360=V360),
                  stone("p2", "LG600000002", "IGI", 160000),                                   # no 360 fields
                  stone("p3", "LG600000003", "IGI", 170000, v360={**V360, "frame_count": 20}),  # too few
                  stone("p4", "LG600000004", "IGI", 180000, v360={**V360, "url": FOLDER + "4"})]
    stone_source.Client._schema_cache.clear()
    spin_capture._cache.clear()
    seen = {"frames": [], "bad_frames": set()}

    def frame_index(url):
        m = re.fullmatch(re.escape(FOLDER) + r"4?/(\d+)\.webp", url)
        return int(m.group(1)) if m else None

    def is_image(url):
        i = frame_index(url)
        return i is not None and i < 240

    def fetch(url):
        seen["frames"].append(url)
        if url in seen["bad_frames"] or frame_index(url) is None:
            return None                                                   # 404
        return JPEG

    def rehost(sb_url, key, items):          # a viewer page becomes our private /v/ link
        return [({"url": VIEWER if v else "", "how": "viewer" if v else "dropped", "src": v, "note": ""},
                 {"url": quote_media.MEDIA_BASE + "b" * 32 + ".jpg" if i else "", "how": "hosted" if i else "dropped",
                  "note": ""}) for v, i in items]
    monkeypatch.setattr(spin_capture, "_is_image", is_image)
    monkeypatch.setattr(spin_capture, "_fetch", fetch)
    monkeypatch.setattr(quote_media, "ALLOW_PRIVATE_HOSTS", True)          # the fake host has no DNS
    monkeypatch.setattr(quote_media, "process_stones", rehost)
    monkeypatch.setattr(spin_jobs, "version_check", lambda force=False: (True, "test"))
    return sb, api, seen


def save(client, number, wait=True):
    """Saves a quote from Live Search. The save itself never waits for 360 capture; with `wait`,
    this then waits for the background captures and re-runs the page (as its auto-refresh does)."""
    at = boot()
    at.session_state["ls_client_sel"] = client
    at.radio(key="ls_mode").set_value("Look up stones").run()
    at.text_area(key="ls_lk_text").input(number).run()
    at.button(key="ls_lk_go").click().run()
    at.button(key="ls_add_quote").click().run()
    assert not at.exception, at.exception
    if wait:
        wait_captures(at)
    return at


def wait_captures(at, timeout=20):
    qid = at.session_state["capture_qid"] if "capture_qid" in at.session_state else None
    if qid:
        t0 = time.time()
        while True:
            done, total = spin_jobs.progress(qid)
            if done >= total:
                break
            assert time.time() - t0 < timeout, "captures didn't finish"
            time.sleep(0.05)
        time.sleep(0.2)                              # the quote is patched right after the status
        at.run()
    return at


def no_viewer_page_read(sb):
    return not any("/view.html" in u for _, u in sb.calls)


def test_mode_values():
    assert spin_jobs.mode("") == "off" and spin_jobs.mode(None) == "off" and spin_jobs.mode("false") == "off"
    assert spin_jobs.mode("test") == "test" and spin_jobs.mode(" TEST ") == "test"
    assert spin_jobs.mode("true") == "on" and spin_jobs.mode("yes") == "off"


def test_capture_allowed(monkeypatch):
    monkeypatch.setattr(spin_jobs, "SPIN_MODE", "test")
    assert spin_jobs.capture_allowed("Pure Carbon Group") and spin_jobs.capture_allowed(" pure carbon group ")
    assert not spin_jobs.capture_allowed("Harlings") and not spin_jobs.capture_allowed("")
    monkeypatch.setattr(spin_jobs, "SPIN_MODE", "off")
    assert not spin_jobs.capture_allowed("Pure Carbon Group")
    monkeypatch.setattr(spin_jobs, "SPIN_MODE", "on")
    assert spin_jobs.capture_allowed("Harlings")


def test_pcg_quote_is_captured_from_api_fields(spin_env):
    sb, api, seen = spin_env
    at = save("Pure Carbon Group", "LG600000001")
    saved = sb.tables["quotes"][-1]
    s = saved["stones"][0]
    assert s["video_url"] == "" and s["media_ref"] == VIEWER
    sp = s["spin"]
    assert sp["v"] == spin_capture.CAPTURE_VERSION == 5 and sp["n"] == 72
    # thinned to 72, top_index included, the spinner starts on it
    idx, top_pos = spin_capture.pick(240, 37)
    assert sp["top"] == top_pos and 37 in idx
    frames = [u for u in seen["frames"] if u.startswith(FOLDER)]
    # only the kept frames are downloaded (never the full 240), top_index among them
    assert sorted(frames) == sorted(f"{FOLDER}/{i}.webp" for i in idx) and f"{FOLDER}/37.webp" in frames
    assert all(re.fullmatch(re.escape(FOLDER) + r"/\d+\.webp", u) for u in seen["frames"])   # own folder only
    stored = {n: d for n, d in sb.storage["quote-media"].items() if n.startswith(sp["id"] + "/")}
    assert sorted(stored) == [f"{sp['id']}/{i:03d}.webp" for i in range(72)]
    for d in stored.values():                                         # WebP, at most 720 px wide, no metadata
        im = Image.open(io.BytesIO(d))
        assert im.format == "WEBP" and im.size[0] <= 720 and not im.info.get("exif") and not im.info.get("icc_profile")
    ups = [h for m, u, h in sb.headers if m == "POST" and f"/quote-media/{sp['id']}/" in u]
    assert len(ups) == 72 and all(h["Cache-Control"] == "public, max-age=31536000, immutable"
                                  and h["Content-Type"] == "image/webp" for h in ups)
    assert s["image_url"].startswith(quote_media.MEDIA_BASE)
    assert no_viewer_page_read(sb)
    t = texts(at)
    assert "360 capture log" in t and "360 processing: 1 of 1 done" in t
    assert "method: API fields (certificate.v360)" in t
    assert "72 frames (thinned evenly from 240)" in t
    assert re.search(r"top\\?_index 37 \(frame %d of the spinner, where it starts\)" % top_pos, t)
    assert re.search(r"time: fetch [\d.]+s, re-encode [\d.]+s, upload [\d.]+s, total [\d.]+s", t)
    assert re.search(r"page weight [\d.]+ MB \(\d+ KB a frame; first view \d+ KB\)", t)
    media_note = [c.value for c in at.caption if c.value.startswith("Media note")]
    assert media_note and "360" not in media_note[0]                 # shown once, in the capture log
    # the quote page shows it (test mode, test client, current code)
    out = get_quote(saved, "test")
    assert out["stones"][0]["spin"]["id"] == sp["id"] and "video_url" not in out["stones"][0]


def test_save_never_waits_for_capture(spin_env, monkeypatch):
    """The quote is saved with its /v/ link at once; the spinner is swapped in when capture ends."""
    sb, api, seen = spin_env
    import threading
    gate = threading.Event()
    real = spin_capture._fetch
    monkeypatch.setattr(spin_capture, "_fetch", lambda url: gate.wait(10) and real(url))
    t0 = time.time()
    at = save("Pure Carbon Group", "LG600000001", wait=False)
    assert time.time() - t0 < 15
    s = sb.tables["quotes"][-1]["stones"][0]
    assert s["video_url"] == VIEWER and "spin" not in s                 # saved before capture finished
    t = texts(at)
    assert "360 processing: 0 of 1 done" in t and at.session_state["quote_link"]
    gate.set()
    wait_captures(at)
    s = sb.tables["quotes"][-1]["stones"][0]
    assert s["spin"]["n"] == 72 and s["video_url"] == "" and s["media_ref"] == VIEWER
    assert "360 processing: 1 of 1 done" in texts(at)


def test_three_stones_are_captured_in_parallel(spin_env, monkeypatch):
    """All stones of a quote run at once: 3 stones take about as long as 1."""
    sb, api, seen = spin_env
    api.stones = [stone(f"q{k}", f"LG70000000{k}", "IGI", 150000 + k, v360={**V360, "url": FOLDER + str(k)})
                  for k in (1, 2, 3)]
    stone_source.Client._schema_cache.clear()
    monkeypatch.setattr(spin_capture, "_is_image", lambda u: bool(re.search(r"/(\d+)\.webp$", u)))

    import threading
    lock, active, most = threading.Lock(), {}, [0]

    def slow(url):
        folder = url.rsplit("/", 1)[0]
        with lock:
            active[folder] = active.get(folder, 0) + 1
            most[0] = max(most[0], sum(1 for v in active.values() if v))   # stones with frames in flight
        time.sleep(0.05)                                # 50 ms a frame
        with lock:
            active[folder] -= 1
        return JPEG
    monkeypatch.setattr(spin_capture, "_fetch", slow)
    t0 = time.time()
    at = save("Pure Carbon Group", "LG700000001 LG700000002 LG700000003")
    took = time.time() - t0
    stones = sb.tables["quotes"][-1]["stones"]
    assert len(stones) == 3 and all(x.get("spin", {}).get("n") == 72 for x in stones)
    assert "360 processing: 3 of 3 done" in texts(at)
    assert most[0] == 3                                 # all three stones were fetching at the same time
    assert took < 30


def test_other_client_behaves_as_capture_off(spin_env):
    sb, api, seen = spin_env
    at = save("Harlings", "LG600000001")
    s = sb.tables["quotes"][-1]["stones"][0]
    assert s["video_url"] == VIEWER and "spin" not in s
    assert not seen["frames"]
    t = texts(at)
    assert "360 capture" not in t


def test_off_by_default_even_for_pcg(spin_env, monkeypatch):
    sb, api, seen = spin_env
    monkeypatch.setenv("SPIN_ENABLED", "")
    save("Pure Carbon Group", "LG600000001")
    s = sb.tables["quotes"][-1]["stones"][0]
    assert s["video_url"] == VIEWER and "spin" not in s and not seen["frames"]


@pytest.mark.parametrize("number, why", [
    ("LG600000002", "no 360 API fields"),
    ("LG600000003", "under the 24-frame minimum"),
])
def test_missing_or_short_api_fields_keep_the_viewer_link(spin_env, number, why):
    sb, api, seen = spin_env
    at = save("Pure Carbon Group", number)
    s = sb.tables["quotes"][-1]["stones"][0]
    assert s["video_url"] == VIEWER and "spin" not in s
    assert not seen["frames"] and no_viewer_page_read(sb)
    t = texts(at)
    assert "360 capture log" in t and why in t


def test_top_frame_must_download(spin_env):
    sb, api, seen = spin_env
    seen["bad_frames"].add(f"{FOLDER}4/37.webp")
    at = save("Pure Carbon Group", "LG600000004")
    s = sb.tables["quotes"][-1]["stones"][0]
    assert s["video_url"] == VIEWER and "spin" not in s
    assert "top frame (top_index) wouldn't download" in texts(at)
    assert not [n for n in sb.storage["quote-media"] if "/" in n]      # nothing stored


def test_frames_from_another_folder_are_refused():
    fs = spin_capture.FrameSet([f"{FOLDER}/{i}.webp" for i in range(30)] + ["https://x.test/other/1.webp"],
                               "p", "API fields", folder=FOLDER)
    assert not spin_capture.own_folder(fs)
    fs.urls = fs.urls[:-1]
    assert spin_capture.own_folder(fs)
    assert not spin_capture.own_folder(spin_capture.FrameSet(fs.urls, "p", "x", folder=""))


def test_viewer_page_is_never_read(monkeypatch):
    """Without API fields and with a viewer URL that isn't a certificate link, nothing is fetched."""
    calls = []
    monkeypatch.setattr(spin_capture.requests, "get", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError))
    spin_capture._cache.clear()
    res = spin_capture.capture("https://db.test", "k", "https://viewer.test/page.html?x=1", None)
    assert not res["ok"] and "no 360 API fields" in res["note"] and not calls
    for name in ("_from_page", "_analyse", "_from_hint", "_VIDEO_FILE", "_get_text"):
        assert not hasattr(spin_capture, name), name


def test_only_current_code_captures_are_trusted():
    assert spin_jobs.trusted_spin({"id": "a" * 32, "n": 120, "top": 3, "v": 4})
    assert not spin_jobs.trusted_spin({"id": "a" * 32, "n": 120, "top": 3, "v": 3})
    assert not spin_jobs.trusted_spin({"id": "a" * 32, "n": 120, "top": 3})          # old v2 rule (top only)
    assert not spin_jobs.trusted_spin({"id": "a" * 32, "n": 20, "top": 3, "v": 4})


# ── Netlify functions ────────────────────────────────────────────────────────
def _node(js, mode):
    env = {**os.environ, "SPIN_ENABLED": mode, "SUPABASE_URL": "https://db.test", "SUPABASE_SERVICE_KEY": "k"}
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True, env=env).stdout)


def get_quote(row, mode, rows_for_links=()):
    js = f"""
    const row = {json.dumps({'client': row['client'], 'stones': row['stones'], 'expires_at': '2099-01-01T00:00:00Z'})};
    global.fetch = async (u) => ({{ ok: true, status: 200,
      json: async () => (String(u).includes('/quotes?') ? [row] : {json.dumps(list(rows_for_links))}) }});
    require('{ROOT}/netlify/functions/get-quote.js').handler({{ path: '/get-quote/abcd1234' }})
      .then(r => process.stdout.write(r.body));
    """
    return _node(js, mode)


SPIN4 = {"id": "c" * 32, "n": 120, "top": 5, "v": 4}
SPIN3 = {"id": "d" * 32, "n": 120, "top": 5, "v": 3}
FRAME4 = quote_media.MEDIA_BASE + "c" * 32 + "/005.jpg"


def _stone(sp, img):
    return {"cert_last4": "0001", "video_url": "", "media_ref": VIEWER, "spin": sp, "image_url": img}


@pytest.mark.parametrize("mode, client, shown", [
    ("test", "Pure Carbon Group", True),
    ("test", "Harlings", False),
    ("", "Pure Carbon Group", False),
    ("true", "Harlings", True),
])
def test_get_quote_modes(mode, client, shown):
    out = get_quote({"client": client, "stones": [_stone(SPIN4, FRAME4)]}, mode)["stones"][0]
    if shown:
        assert out["spin"]["id"] == SPIN4["id"] and "video_url" not in out
    else:
        assert "spin" not in out and out["video_url"] == VIEWER and "image_url" not in out


def test_get_quote_drops_older_captures_in_test_mode():
    img = quote_media.MEDIA_BASE + "d" * 32 + "/005.jpg"
    out = get_quote({"client": "Pure Carbon Group", "stones": [_stone(SPIN3, img)]}, "test")["stones"][0]
    assert "spin" not in out and out["video_url"] == VIEWER and "image_url" not in out


def _fn(name, query, rows, mode):
    js = f"""
    global.fetch = async () => ({{ ok: true, status: 200, json: async () => {json.dumps(rows)} }});
    require('{ROOT}/netlify/functions/{name}.js').handler({{ httpMethod: 'GET', queryStringParameters: {json.dumps(query)} }})
      .then(r => process.stdout.write(JSON.stringify({{ code: r.statusCode, body: JSON.parse(r.body) }})));
    """
    return _node(js, mode)


ROW4 = {"token": "abcdefgh23", "vendor_url": "https://viewer.test/x", "spin_id": "c" * 32, "spin_frames": 120,
        "spin_top": 5, "spin_version": 4}


def test_viewer_link_never_shows_frames_in_test_mode():
    assert _fn("media-link", {"t": "abcdefgh23"}, [ROW4], "test")["body"] == {"url": "https://viewer.test/x"}
    assert _fn("media-link", {"t": "abcdefgh23"}, [ROW4], "true")["body"]["spin"]["id"] == "c" * 32
    old = {**ROW4, "spin_version": 3}
    assert _fn("media-link", {"t": "abcdefgh23"}, [old], "true")["body"] == {"url": "https://viewer.test/x"}


def test_share_link_fallback_in_test_mode():
    # the exact current-code capture in the link: shown
    assert _fn("spin-fallback", {"id": "c" * 32}, [ROW4], "test")["body"]["spin"]["id"] == "c" * 32
    # an older capture in the link: never swapped for a newer one in test mode, original viewer instead
    assert _fn("spin-fallback", {"id": "d" * 32}, [ROW4], "test")["body"] == {"token": "abcdefgh23"}
    assert _fn("spin-fallback", {"id": "c" * 32}, [ROW4], "")["body"] == {"token": "abcdefgh23"}


def test_versions_match_between_app_and_site():
    js = open(os.path.join(ROOT, "netlify/functions/lib/spin.js")).read()
    assert f"const CAPTURE_VERSION = {spin_capture.CAPTURE_VERSION};" in js
    assert f"const TRUSTED_VERSION = {spin_capture.TRUSTED_VERSION};" in js
    assert "value:" not in open(os.path.join(ROOT, "render.yaml")).read().split("key: SPIN_ENABLED")[1]


def test_setup_sql_allows_webp_and_the_fake_bucket_enforces_it():
    from fakesb import bucket_mime_types
    allowed = bucket_mime_types()
    assert "image/webp" in allowed and {"image/jpeg", "image/png", "video/mp4", "video/webm"} <= set(allowed)


def test_webp_upload_accepted_by_the_bucket_as_set_up(spin_env):
    sb, api, seen = spin_env
    save("Pure Carbon Group", "LG600000001")
    assert any(n.endswith(".webp") for n in sb.storage["quote-media"])


def test_storage_refusal_message_is_in_capture_log_and_timing_log(spin_env, capfd):
    """A bucket that doesn't allow WebP: storage's message (masked) reaches the capture log and [spin-timing]."""
    sb, api, seen = spin_env
    sb.allowed_mime["quote-media"] = ["image/jpeg", "image/png", "video/mp4", "video/webm"]      # the old setup
    at = save("Pure Carbon Group", "LG600000001")
    s = sb.tables["quotes"][-1]["stones"][0]
    assert "spin" not in s or not s["spin"]                                                    # viewer link kept
    t = texts(at)
    assert "frame upload failed (upload HTTP 415" in t and "mime type image/webp is not supported" in t
    out = capfd.readouterr().out
    line = [ln for ln in out.splitlines() if ln.startswith("[spin-timing] ")]
    assert len(line) == 1
    d = json.loads(line[0][len("[spin-timing] "):])
    assert d["ok"] is False and "HTTP 415" in d["error"] and "image/webp is not supported" in d["error"]
    assert d["fetch_s"] >= 0 and d["encode_s"] >= 0 and "total_s" in d
    assert not any(n.endswith(".webp") for n in sb.storage["quote-media"])                     # nothing stored


def test_storage_error_text_is_masked():
    class R:
        status_code = 400
        text = ""

        def json(self):
            return {"statusCode": "400", "error": "Bad", "message":
                    "cannot reach https://srlbevzrkovruyerixdi.supabase.co/storage/v1/object/quote-media/"
                    "0123456789abcdef0123456789abcdef/000.webp via nivoda-cdn"}
    m = spin_capture.storage_error(R())
    assert m.startswith("upload HTTP 400: Bad: ") and "supabase.co" not in m and "0123456789abcdef" not in m
    assert "nivoda" not in m.lower() and "[host]" in m and len(m) < 200
