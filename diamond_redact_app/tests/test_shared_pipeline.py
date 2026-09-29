"""One shared media + certificate pipeline: Create quote and Live Search give the same results for the
same stone; the Loupe360 link parsing; each 360 lookup route (own fields, main API, public endpoint);
certificate failure reasons and manual upload."""
import io
import json
import os
import re

import fitz
import pytest
from PIL import Image

import cert_attach
import certs
import fakecerts as F
import quote_media
import spin_capture as sc
import spin_jobs
import stone_source
from fakeapi import HOST, schema, stone
from fakesb import SB, FakeSupabase, Resp
from test_app import PDFS, boot, env, texts  # noqa: F401  (env is a fixture)

CERT_ID = "f19fae88-7404-5b36-9645-6104a44b4250"
LINK = f"https://loupe360.com/diamond/{CERT_ID}/video/500/500/autoplay?type=360"
BASE = "https://assets-images.pixorac.com/aHR0cHM6Ly92MzYwLmluL0xHMi92aXNpb24zNjAuaHRtbD9kPVMtMjI4OS0yMw=="
STILL = "https://assets-images.pixorac.com/still-7531752625.jpg"
# The real answer of the viewer's own data request (see the task text)
REAL = {"data": {"certificate": {
    "id": CERT_ID, "shape": "ROUND", "certNumber": "7531752625", "image": STILL,
    "v360": {"top_index": "181", "frame_count": 256, "url": BASE},
    "product_videos": [{"id": "1", "url": BASE, "display_index": 0, "loupe360_url": LINK, "type": "360",
                        "top_index": "181", "frame_count": 256}]}}}
JPEG = (lambda b: (Image.new("RGB", (40, 30), (200, 10, 10)).save(b, "JPEG"), b.getvalue())[1])(io.BytesIO())
OWN = {"top_index": "181", "frame_count": 256, "url": BASE}


# ── Link parsing ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("link", [
    f"https://loupe360.com/diamond/{CERT_ID}/video/500/500/autoplay?type=360",
    f"https://loupe360.com/diamond/{CERT_ID}/video/500/500",
    f"https://loupe360.com/diamond/{CERT_ID}/video/500/500/",
    f"https://loupe360.com/diamond/{CERT_ID}",
    f"https://loupe360.com/diamond/{CERT_ID}/",
    f"https://loupe360.com/diamond/{CERT_ID}?type=360&autoplay=1",
    f"https://loupe360.com/diamond/{CERT_ID}/video/500/500/autoplay?type=360#top",
    f"  https://www.loupe360.com/diamond/{CERT_ID}/autoplay  ",
    f"loupe360.com/diamond/{CERT_ID}/video/1/1",
    f"http://x.test/a/diamond/{CERT_ID}/b",
])
def test_certificate_id_is_found_in_any_loupe360_link(link):
    assert sc.cert_id_from_link(link) == CERT_ID


@pytest.mark.parametrize("link", ["", None, "https://loupe360.com/", "https://loupe360.com/diamond/",
                                  "https://viewer.test/page.html?id=" + CERT_ID, "https://loupe360.com/diamond/x",
                                  "https://loupe360.com/video/" + CERT_ID])
def test_links_without_a_certificate_id(link):
    assert sc.cert_id_from_link(link) == ""


# ── The three lookup routes ───────────────────────────────────────────────────
@pytest.fixture
def frames(monkeypatch):
    """256 frames under BASE, a fake frame server, and no real network."""
    seen = {"frames": []}
    fake = FakeSupabase()

    def idx(url):
        m = re.fullmatch(re.escape(BASE) + r"/(\d+)\.webp", url)
        return int(m.group(1)) if m else None
    monkeypatch.setattr(sc, "_is_image", lambda u: idx(u) is not None and idx(u) < 256)
    monkeypatch.setattr(sc, "_fetch", lambda u: seen["frames"].append(u) or (JPEG if idx(u) is not None else None))
    monkeypatch.setattr(quote_media, "ALLOW_PRIVATE_HOSTS", True)
    fake.install(monkeypatch)
    monkeypatch.setattr(sc, "CERT_LOOKUP", lambda cid: (None, "certificate lookup: no certificate with that ID"))
    monkeypatch.setattr(sc, "PUBLIC_LOOKUP", None)
    sc._cache.clear()
    posts = []

    class Http:                              # the shared session, except that the public endpoint is answered here
        def __getattr__(self, name):
            return getattr(real_http, name)

        def post(self, url, **kw):
            if url == sc.PUBLIC_URL:
                posts.append((url, kw))
                return seen["public"](url, kw)
            return real_http.post(url, **kw)
    real_http = sc._http
    monkeypatch.setattr(sc, "_http", Http())
    seen.update(fake=fake, posts=posts, public=lambda url, kw: Resp(200, REAL))
    return seen


def test_route_c_public_endpoint_matches_the_real_response(frames):
    """The real case: no fields of its own, the main API has nothing, the public endpoint has the data."""
    res = sc.capture(SB, "k", LINK, None)
    assert res["ok"], res["facts"]
    # the exact request: the query as given, the certificate ID as a variable, no sign-in, no redirects followed
    (url, kw), = frames["posts"]
    assert kw["json"] == {"query": sc.PUBLIC_QUERY, "variables": {"cert_id": CERT_ID}}
    assert "certificate_by_cert_id(cert_id: $cert_id)" in sc.PUBLIC_QUERY
    assert "v360 { top_index frame_count url }" in sc.PUBLIC_QUERY
    assert "product_videos { id url display_index loupe360_url type top_index frame_count }" in sc.PUBLIC_QUERY
    assert not {k.lower() for k in kw["headers"]} & {"authorization", "cookie", "x-api-key"}
    assert kw["allow_redirects"] is False
    # 256 frames thinned to 72, from the stone's own folder only, top_index 181 included and where the spinner starts
    assert res["log"]["source_frames"] == 256 and res["log"]["frames"] == 72 and res["log"]["top_index"] == 181
    assert all(re.fullmatch(re.escape(BASE) + r"/\d+\.webp", u) for u in frames["frames"])
    assert "181" in {u.rsplit("/", 1)[1].split(".")[0] for u in frames["frames"]}
    assert res["still"] == STILL                                    # the response's image is the still
    facts = "\n".join(res["facts"])
    assert f"certificate ID {CERT_ID}" in facts                                       # the extracted ID is logged
    assert "step a (stone's own search-result fields): none" in facts
    assert "step b (main API by certificate ID): failed — certificate lookup: no certificate with that ID" in facts
    assert "step c (public 360 endpoint)" in facts and "256 frames, top frame 181" in facts
    assert res["method"] == "step c (public 360 endpoint)"
    assert not any("loupe360" in u for _, u in frames["fake"].calls)                  # the viewer page is never read


def test_route_c_product_videos_only(frames):
    only_pv = json.loads(json.dumps(REAL))
    del only_pv["data"]["certificate"]["v360"]
    frames["public"] = lambda url, kw: Resp(200, only_pv)
    res = sc.capture(SB, "k", LINK, None)
    assert res["ok"] and "certificate.product_videos" in "\n".join(res["facts"])
    assert res["log"]["top_index"] == 181


def test_route_a_own_fields_first_and_nothing_else_is_called(frames):
    for hint, where in (({"certificate": {"v360": OWN, "image": STILL}}, "certificate.v360"),
                        ({"certificate": {"product_videos": [{"type": "360", **OWN}]}}, "certificate.product_videos"),
                        ({"v360": OWN}, "v360"),
                        ({"product_videos": [{"type": "360", **OWN}]}, "product_videos")):
        sc._cache.clear()
        frames["posts"].clear()
        res = sc.capture(SB, "k", LINK, hint)
        assert res["ok"] and where in "\n".join(res["facts"]), (where, res["facts"])
        assert res["method"] == "step a (stone's own search-result fields)" and not frames["posts"]


def test_route_b_main_api_before_the_public_endpoint(frames, monkeypatch):
    calls = []
    monkeypatch.setattr(sc, "CERT_LOOKUP", lambda cid: calls.append(cid) or ({"certificate": {"v360": OWN, "image": STILL}}, "ok"))
    res = sc.capture(SB, "k", LINK, None)
    assert res["ok"] and calls == [CERT_ID] and not frames["posts"]
    assert res["method"] == "step b (main API by certificate ID)"


def test_an_unusable_route_a_falls_through_to_the_next(frames):
    bad = {"certificate": {"v360": {**OWN, "frame_count": 20}}}                      # under the 24-frame minimum
    res = sc.capture(SB, "k", LINK, bad)
    facts = "\n".join(res["facts"])
    assert res["ok"] and "under the 24-frame minimum" in facts and "step a: failed" in facts
    assert res["method"] == "step c (public 360 endpoint)"


def test_every_route_failing_says_why_for_each(frames):
    frames["public"] = lambda url, kw: Resp(200, {"data": {"certificate": None}})
    res = sc.capture(SB, "k", LINK, None)
    assert not res["ok"]
    facts = "\n".join(res["facts"])
    assert "step a (stone's own search-result fields): none" in facts
    assert "step b (main API by certificate ID): failed — certificate lookup: no certificate with that ID" in facts
    assert "step c (public 360 endpoint): failed — public endpoint: no certificate with that ID" in facts
    assert "no 360 frames from any route" in res["note"] and "viewer pages are never read" in res["note"]


def test_link_without_certificate_id_skips_lookups(frames):
    res = sc.capture(SB, "k", "https://viewer.test/page.html?x=1", None)
    facts = "\n".join(res["facts"])
    assert not res["ok"] and "no certificate ID found" in facts and "steps b and c skipped" in facts
    assert not frames["posts"]


@pytest.mark.parametrize("answer, why", [
    (lambda: Resp(500, {}), "public endpoint HTTP 500"),
    (lambda: Resp(302, {}), "public endpoint HTTP 302"),
    (lambda: Resp(200, content=b"<html>nope</html>"), "public endpoint answer isn't JSON"),
    (lambda: Resp(200, {"errors": [{"message": "boom"}]}), "public endpoint: boom"),
    (lambda: Resp(200, {"data": {"certificate": {"id": CERT_ID, "certNumber": "1"}}}), "no v360 / product_videos"),
])
def test_public_endpoint_failures_are_reported(frames, answer, why):
    frames["public"] = lambda url, kw: answer()
    res = sc.capture(SB, "k", LINK, None)
    assert not res["ok"] and why in "\n".join(res["facts"])


def test_public_endpoint_frames_under_the_minimum_are_refused(frames):
    few = json.loads(json.dumps(REAL))
    for k in ("v360",):
        few["data"]["certificate"][k]["frame_count"] = 20
    few["data"]["certificate"]["product_videos"][0]["frame_count"] = 20
    frames["public"] = lambda url, kw: Resp(200, few)
    res = sc.capture(SB, "k", LINK, None)
    assert not res["ok"] and "under the 24-frame minimum" in "\n".join(res["facts"]) and not frames["frames"]


def test_other_stones_certificate_number_is_refused(frames):
    res = sc.capture(SB, "k", LINK, None, expect_cert="LG833689789")
    assert not res["ok"] and "its certificate number isn't this stone's" in "\n".join(res["facts"])
    ok = sc.capture(SB, "k", LINK, None, expect_cert="7531752625")
    assert ok["ok"]


def test_top_frame_that_wont_download_fails_the_capture(frames, monkeypatch):
    real = sc._fetch
    monkeypatch.setattr(sc, "_fetch", lambda u: None if u.endswith("/181.webp") else real(u))
    res = sc.capture(SB, "k", LINK, None)
    assert not res["ok"] and "top frame (top_index) wouldn't download" in res["note"]


def test_no_supplier_name_in_the_capture_log(frames):
    res = sc.capture(SB, "k", LINK, None)
    assert not re.search(r"nivoda", json.dumps(res), re.I)


def test_the_still_from_the_response_is_used_when_the_stone_has_none(frames, monkeypatch):
    asked = []
    monkeypatch.setattr(quote_media, "process", lambda sb, k, url, want: asked.append((url, want)) or
                        {"how": "hosted", "url": quote_media.MEDIA_BASE + "c" * 32 + ".jpg", "note": ""})
    monkeypatch.setattr(spin_jobs, "known_spin", lambda *a: None)
    res = spin_jobs.capture_one(SB, "k", LINK, None)
    assert res["ok"] and res["still"] == quote_media.MEDIA_BASE + "c" * 32 + ".jpg" and asked == [(STILL, "image")]
    stone_ = {"video_url": quote_media.VIEWER_LINK_BASE + "abcdefgh23", "image_url": ""}
    assert spin_jobs.apply(stone_, res)
    assert stone_["image_url"] == res["still"] and stone_["spin"]["n"] == 72 and stone_["video_url"] == ""


# ── Create quote and Live Search: the same code path, the same results ─────────
@pytest.fixture
def both(env, monkeypatch):
    """The app with 360 capture on for Pure Carbon Group. The frame server and the public endpoint are
    faked; the search API knows the stone but has no 360 fields for it (so the pasted-link route is used)."""
    sb, api = env
    monkeypatch.setenv("SPIN_ENABLED", "test")
    api.schema = schema(v360=True)
    s = stone("z1", "LG833689789", "IGI", 150000)
    s["diamond"]["video"] = LINK
    api.stones = [s]
    stone_source.Client._schema_cache.clear()
    sc._cache.clear()
    viewer = quote_media.VIEWER_LINK_BASE + "abcdefgh23"
    monkeypatch.setattr(quote_media, "ALLOW_PRIVATE_HOSTS", True)
    monkeypatch.setattr(quote_media, "process_stones", lambda sb_, k, items: [
        ({"url": viewer if v else "", "how": "viewer" if v else "dropped", "src": v, "note": ""},
         {"url": "", "how": "dropped", "note": ""}) for v, i in items])
    monkeypatch.setattr(quote_media, "process", lambda *a: {"how": "hosted", "url": quote_media.MEDIA_BASE + "c" * 32 + ".jpg",
                                                              "note": ""})
    monkeypatch.setattr(spin_jobs, "version_check", lambda force=False: (True, "test"))
    idx = lambda u: (int(m.group(1)) if (m := re.fullmatch(re.escape(BASE) + r"/(\d+)\.webp", u)) else None)
    monkeypatch.setattr(sc, "_is_image", lambda u: idx(u) is not None and idx(u) < 256)
    monkeypatch.setattr(sc, "_fetch", lambda u: JPEG if idx(u) is not None else None)
    monkeypatch.setattr(sc, "PUBLIC_LOOKUP", lambda cid: (
        ({"certificate": {**REAL["data"]["certificate"], "certNumber": "LG833689789"}}, "ok")      # this stone's certificate
        if cid == CERT_ID else (None, "no certificate with that ID")))
    return sb, api, viewer


def _wait(at):
    import time
    qid = at.session_state["capture_qid"]
    t0 = time.time()
    while spin_jobs.progress(qid)[0] < spin_jobs.progress(qid)[1]:
        assert time.time() - t0 < 30
        time.sleep(0.05)
    time.sleep(0.3)
    at.run()


def _live_search_quote():
    at = boot()
    at.session_state["ls_client_sel"] = "Pure Carbon Group"
    at.radio(key="ls_mode").set_value("Look up stones").run()
    at.text_area(key="ls_lk_text").input("LG833689789").run()
    at.button(key="ls_lk_go").click().run()
    at.button(key="ls_add_quote").click().run()
    assert not at.exception, at.exception
    _wait(at)
    return at


def _create_quote(pdf, name="IGI-LG833689789.pdf"):
    at = boot()
    at.session_state["q_client_sel"] = "Pure Carbon Group"
    at.run()
    at.file_uploader(key="qpdf_0_0").upload(name, pdf, "application/pdf")
    at.text_input(key="qvid_0_0").input(LINK).run()
    at.button(key="gen_quote").click().run()
    assert not at.exception, at.exception
    _wait(at)
    return at


def test_create_quote_and_live_search_give_the_same_media_and_certificate(both):
    sb, api, viewer = both
    _live_search_quote()
    ls = sb.tables["quotes"][-1]["stones"][0]
    _create_quote(F.igi())
    cq = sb.tables["quotes"][-1]["stones"][0]
    for st_ in (ls, cq):
        assert st_["video_url"] == "" and st_["media_ref"] == viewer
        assert st_["spin"]["n"] == 72 and st_["spin"]["v"] == sc.CAPTURE_VERSION and st_["spin"]["id"]
        assert st_["image_url"] == quote_media.MEDIA_BASE + "c" * 32 + ".jpg"      # the response's still
        assert st_["pdf_url"].startswith(cert_attach.CERT_BASE)
        assert re.fullmatch(r"[a-f0-9]{32}\.pdf", st_["pdf_url"][len(cert_attach.CERT_BASE):])
        assert st_["cert_last4"] == "9789"
    assert ls["spin"]["top"] == cq["spin"]["top"]
    # the same certificate: the same layout, and the same numbers gone
    pdfs = list(sb.storage["certificates"].values())
    assert len(pdfs) == 2
    for pdf in pdfs:
        assert b"833689789" not in pdf
    # both captures used the same route and said the same things
    from spin_jobs import STATUS
    notes = [v for st_ in STATUS.values() for v in st_.values()]
    assert len(notes) >= 2
    for v in notes[-2:]:
        facts = "\n".join(v["facts"])
        assert f"certificate ID {CERT_ID}" in facts and "step c (public 360 endpoint)" in facts


def test_create_quote_certificate_uses_the_shared_verification(both):
    """A certificate that can't be safely redacted stops the Create quote save (fail closed)."""
    sb, api, viewer = both
    n_quotes = len(sb.tables["quotes"])
    bad = F.add_barcode(F.igi(), rect=(300, 20, 396, 35))               # a barcode outside the blanked area
    at = boot()
    at.session_state["q_client_sel"] = "Pure Carbon Group"
    at.run()
    at.file_uploader(key="qpdf_0_0").upload("IGI-LG833689789.pdf", bad, "application/pdf")
    at.text_input(key="qvid_0_0").input(LINK).run()
    at.button(key="gen_quote").click().run()
    assert len(sb.tables["quotes"]) == n_quotes and not sb.storage["certificates"]
    assert "Redaction failed for ···9789" in " ".join(e.value for e in at.error) and "barcode" in " ".join(e.value for e in at.error)


def test_create_quote_source_uses_the_shared_functions():
    src_ = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")).read()
    assert "cert_attach.process(" in src_ and "redact_for_quote(" not in src_ and "upload_pdf(" not in src_.split("def upload_pdf")[1]
    assert src_.count("rehost_media(") >= 2 and "def save_quote" in src_          # one save path for both screens


# ── Certificates: exact failure reasons, layouts, manual upload ────────────────
class _Srv:
    def __init__(self, tmp_path, monkeypatch):
        import http.server
        import threading
        (tmp_path / "ok.pdf").write_bytes(F.gia())
        (tmp_path / "page.html").write_text("<!doctype html><html>report check</html>")
        (tmp_path / "text.pdf").write_text("just some words")

        class H(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=str(tmp_path), **k)

            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/forbidden"):
                    self.send_response(403)
                    self.end_headers()
                    return
                super().do_GET()
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        monkeypatch.setattr(quote_media, "ALLOW_PRIVATE_HOSTS", True)


@pytest.mark.parametrize("path, reason", [
    ("/forbidden.pdf", "download HTTP 403"),
    ("/missing.pdf", "download HTTP 404"),
    ("/page.html", "no PDF provided, online report-check link only"),
    ("/text.pdf", "not a PDF"),
])
def test_certificate_log_says_exactly_why(tmp_path, monkeypatch, path, reason):
    srv = _Srv(tmp_path, monkeypatch)
    try:
        log = cert_attach.process({"url": srv.base + path, "cert_no": "2141438167", "lab": "GIA"}, F.logo(), "sb", "k",
                                  uploader=lambda *a: "x")
    finally:
        srv.srv.shutdown()
    assert log["pdf_url"] == "" and log["reason"] == reason and log["note"] == "Certificate not attached: " + reason


def test_precheck_reports_the_same_reasons_and_caches(tmp_path, monkeypatch):
    srv = _Srv(tmp_path, monkeypatch)
    try:
        ok, why, layout = cert_attach.precheck({"url": srv.base + "/ok.pdf", "cert_no": "2141438167", "lab": "GIA"})
        assert ok and layout == "GIA" and why == ""
        ok, why, _ = cert_attach.precheck({"url": srv.base + "/page.html", "cert_no": "2141438167", "lab": "GIA"})
        assert not ok and why == "no PDF provided, online report-check link only"
        ok, why, _ = cert_attach.precheck({"url": "", "cert_no": "2141438167", "lab": "GIA"})
        assert not ok and "no certificate file" in why
    finally:
        srv.srv.shutdown()
    ok, why, layout = cert_attach.precheck({"url": srv.base + "/ok.pdf", "cert_no": "2141438167", "lab": "GIA"})
    assert ok and layout == "GIA"                                   # answered from the cache, the server is gone


def test_uploaded_pdf_goes_through_the_same_detection_redaction_and_verification():
    up = []
    up_fn = lambda sb, k, data: up.append(data) or cert_attach.CERT_BASE + "0" * 32 + ".pdf"
    link = cert_attach.process({"url": "u", "cert_no": "2141438167", "lab": "GIA"}, F.logo(), "sb", "k",
                               uploader=up_fn, fetch=lambda u: F.gia())
    mine = cert_attach.process({"data": F.gia(), "cert_no": "2141438167", "lab": "GIA"}, F.logo(), "sb", "k", uploader=up_fn)
    assert link["source"] == "found (PDF)" and mine["source"] == "uploaded PDF"
    for k in ("lab_format", "redaction", "verification", "cert_type", "layout"):
        assert link[k] == mine[k]
    assert mine["verification"] == "passed" and mine["pdf_url"]
    # the wrong certificate for the stone, and a PDF that can't be redacted safely, are refused
    wrong = cert_attach.process({"data": F.gia(), "cert_no": "5555555555", "lab": "GIA"}, F.logo(), "sb", "k", uploader=up_fn)
    assert wrong["pdf_url"] == "" and "doesn't match the stone" in wrong["reason"]
    shifted = cert_attach.process({"data": F.gia(qr_shift=30), "cert_no": "2141438167", "lab": "GIA"}, F.logo(), "sb", "k",
                                  uploader=up_fn)
    assert shifted["pdf_url"] == "" and "QR code" in shifted["reason"]
    assert len(up) == 2


HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.mark.parametrize("fn, cert_no, lab, layout", [
    ("igi_photo_LG725041893.pdf", "725041893", "IGI", "IGI Photo"),
    ("gia_dossier_4417250938.pdf", "4417250938", "GIA", "GIA Dossier"),
])
def test_new_layouts_are_detected_and_verified_the_same_from_a_link_or_an_upload(fn, cert_no, lab, layout):
    data = open(os.path.join(HERE, "data", fn), "rb").read()
    a = cert_attach.process({"url": "u", "cert_no": cert_no, "lab": lab}, F.logo(), "sb", "k",
                            uploader=lambda *x: "a", fetch=lambda u: data)
    b = cert_attach.process({"data": data, "cert_no": "", "lab": lab}, F.logo(), "sb", "k", uploader=lambda *x: "b")
    assert a["layout"] == b["layout"] == layout and a["verification"] == b["verification"] == "passed"
    assert b["pdf_url"] == "b"


def test_create_quote_barcode_blanking_uses_the_shared_verification():
    pdf = F.add_barcode(F.igi())
    out = {}
    log = cert_attach.process({"data": pdf, "cert_no": "", "lab": "IGI"}, F.logo(), "sb", "k",
                              uploader=lambda sb, k, d: out.setdefault("pdf", d) and "u")
    assert log["verification"] == "passed" and log["layout"] == "IGI"
    d = fitz.open(stream=out["pdf"], filetype="pdf")
    px = d[0].get_pixmap(clip=cert_attach._rect(certs.ZONES["IGI"]["barcode"]), dpi=72, colorspace=fitz.csGRAY,
                         alpha=False).samples
    assert sum(1 for v in px if v < 200) == 0                        # the barcode area is blank
    wrong_card = cert_attach.process({"data": F.gia(), "cert_no": "", "lab": "IGI"}, F.logo(), "sb", "k",
                                     uploader=lambda *a: "u")
    assert wrong_card["pdf_url"] == "" and "looks like a GIA certificate" in wrong_card["reason"]


# ── Live Search: Upload certificate when the automatic download fails ──────────
@pytest.fixture
def broken_cert(env, monkeypatch):
    sb, api = env
    s = stone("q1", "2141438167", "GIA", 800000, lg=False)
    s["diamond"]["certificate"]["pdfUrl"] = f"{HOST}/reports/2141438167"          # not a file: an online report-check link
    api.stones = [s]
    stone_source.Client._schema_cache.clear()

    def fetch(url):
        raise cert_attach.Skip("Certificate not attached: no PDF provided, online report-check link only")
    monkeypatch.setattr(cert_attach, "download", fetch)
    return sb, api


def _lookup(at, number):
    at.radio(key="ls_mode").set_value("Look up stones").run()
    at.text_area(key="ls_lk_text").input(number).run()
    at.button(key="ls_lk_go").click().run()
    assert not at.exception, at.exception


def test_live_search_offers_upload_when_the_certificate_cant_be_pulled(broken_cert):
    sb, api = broken_cert
    at = boot()
    _lookup(at, "2141438167")
    t = texts(at)
    assert "Certificate can't be pulled automatically: no PDF provided, online report-check link only" in t
    assert "Upload certificate (PDF)" in t
    sid = "q1"
    # a PDF that doesn't fit is refused on the spot and nothing is kept
    at.file_uploader(key=f"ls_cup_{sid}").upload("x.pdf", F.igi(), "application/pdf")
    at.run()
    assert "This PDF can't be used" in " ".join(e.value for e in at.error)
    assert not at.session_state["ls_cert_up"]
    # the right one is kept, and attached (redacted, logo, verified) when the quote is saved
    at.file_uploader(key=f"ls_cup_{sid}").upload("gia.pdf", F.gia(), "application/pdf")
    at.run()
    assert at.session_state["ls_cert_up"][sid]["name"] == "gia.pdf"
    assert "Uploaded certificate: gia.pdf" in texts(at)
    at.button(key="ls_add_quote").click().run()
    assert not at.exception, at.exception
    saved = sb.tables["quotes"][-1]["stones"][0]
    assert saved["pdf_url"].startswith(cert_attach.CERT_BASE) and "_cert" not in json.dumps(saved)
    pdf = next(iter(sb.storage["certificates"].values()))
    assert b"2141438167" not in pdf
    log = at.session_state["cert_log"][0]
    assert log["source"] == "uploaded PDF" and log["verification"] == "passed"


def test_without_an_upload_the_failure_reason_is_in_the_certificate_log(broken_cert):
    sb, api = broken_cert
    at = boot()
    _lookup(at, "2141438167")
    at.button(key="ls_add_quote").click().run()
    saved = sb.tables["quotes"][-1]["stones"][0]
    assert "pdf_url" not in saved or not saved["pdf_url"]
    log = at.session_state["cert_log"][0]
    assert log["reason"] == "no PDF provided, online report-check link only"
    assert "no PDF provided, online report-check link only" in texts(at)


def test_new_search_clears_uploads_and_overrides(broken_cert):
    sb, api = broken_cert
    at = boot()
    _lookup(at, "2141438167")
    at.file_uploader(key="ls_cup_q1").upload("gia.pdf", F.gia(), "application/pdf")
    at.run()
    at.text_input(key="ls_ov_q1").input("4000").run()
    assert at.session_state["ls_cert_up"] and at.session_state["ls_over"] == {"q1": "4000"}
    at.button(key="ls_new_search").click().run()
    assert not at.session_state["ls_cert_up"] and not at.session_state["ls_over"]
