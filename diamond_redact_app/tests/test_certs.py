"""Part 2: certificates for Live Search stones — redaction, verification, fail-closed."""
import http.server
import os
import re
import subprocess
import threading

import fitz
import pytest

import cert_attach as C
import certs
import fakecerts as F
import quote_media

UP = []


class no_barcode_zone:
    """The original layouts changed in exactly one way since they were first written: the IGI layout
    now also blanks its barcode. With that one zone taken out, output must match the old code byte for byte."""
    def __enter__(self):
        self.z = certs.ZONES["IGI"].pop("barcode")

    def __exit__(self, *a):
        certs.ZONES["IGI"]["barcode"] = self.z


def uploader(sb, key, data):
    UP.append(data)
    return C.CERT_BASE + "0" * 32 + ".pdf"


def run(data, cert_no, lab="GIA"):
    return C.process({"url": "https://x/c.pdf", "cert_no": cert_no, "lab": lab}, F.logo(), "sb", "k",
                     uploader=uploader, fetch=lambda u: data)


def _text(pdf):
    d = fitz.open(stream=pdf, filetype="pdf")
    t = "\n".join(p.get_text() for p in d)
    meta = dict(d.metadata)
    n = d.embfile_count()
    d.close()
    return t, meta, n


def test_create_quote_redaction_unchanged():
    """certs.redact_pdf is byte-for-byte the Create quote code it was moved from (commit 90a4d32)."""
    old = subprocess.run(["git", "show", "90a4d32:diamond_redact_app/app.py"], capture_output=True,
                         text=True, check=True).stdout
    zones_src = old[old.index("CERT_ZONES = {"):old.index("PADDING       = 1.5")]
    fn_src = old[old.index("def redact_pdf("):old.index("import re as _re")]
    ns = {"fitz": fitz, "io": __import__("io"), "IGI_B64": "", "GIA_B64": ""}
    exec(zones_src + "PADDING = 1.5\n" + fn_src, ns)
    no_id = lambda b: b.rsplit(b"trailer", 1)[0]      # the trailer holds the file ID, random per save
    with no_barcode_zone():
        for ctype, pdf in (("GIA", F.gia()), ("GIA Colour", F.gia(colour=True)), ("IGI", F.igi())):
            assert no_id(ns["redact_pdf"](pdf, ctype, F.logo())) == no_id(certs.redact_pdf(pdf, ctype, F.logo()))


@pytest.mark.parametrize("name,pdf,cert_no,lab,ctype", [
    ("GIA", F.gia(), "2141438167", "GIA", "GIA"),
    ("GIA colour", F.gia(colour=True), "2141438167", "GIA", "GIA Colour"),
    ("IGI with LG", F.igi(), "LG833689789", "IGI", "IGI"),
    ("IGI without LG", F.igi(), "833689789", "IGI", "IGI"),
])
def test_redacted_and_verified(name, pdf, cert_no, lab, ctype):
    UP.clear()
    log = run(pdf, cert_no, lab)
    assert log["verification"] == "passed", log
    assert log["pdf_url"].startswith("https://quote.alldiamondeverything.com/certs/")
    assert log["cert_type"] == ctype and log["redaction"] == "applied" and log["source"] == "found (PDF)"
    text, meta, n_att = _text(UP[0])
    core = re.sub(r"\D", "", cert_no)
    assert core not in re.sub(r"\D", "", text)
    left = [r for r in re.findall(r"\d+", text) if core.endswith(r)]
    assert left and all(len(r) <= 4 for r in left)               # only the end of the number is still shown
    assert not any(v for k, v in meta.items() if k not in ("format", "encryption"))   # metadata stripped
    assert n_att == 0                                             # attachment removed
    assert core.encode() not in UP[0] and core.encode() not in fitz.open(stream=UP[0]).tobytes(expand=255)


def test_must_fail_verification():
    """Number inside the layout's area but past the masked part: redaction leaves it — not attached."""
    UP.clear()
    log = run(F.gia(tiny_right=True), "2141438167")
    assert log["verification"].startswith("FAILED") and "text layer" in log["verification"]
    assert log["note"] == "Certificate not attached: redaction couldn't be verified"
    assert log["pdf_url"] == "" and not UP                        # nothing stored


def test_image_only_certificate(tmp_path, monkeypatch):
    """A JPG certificate: downloaded, recognised as an image, not attached (real download path)."""
    (tmp_path / "c.jpg").write_bytes(F.jpg())
    (tmp_path / "c.pdf").write_bytes(F.gia())
    (tmp_path / "c.html").write_text("<html>viewer</html>")

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(tmp_path), **k)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setattr(quote_media, "ALLOW_PRIVATE_HOSTS", True)
    try:
        log = C.process({"url": base + "/c.jpg", "cert_no": "2141438167", "lab": "GIA"}, F.logo(), "sb", "k",
                        uploader=uploader)
        assert log["pdf_url"] == "" and "not a PDF (the certificate is an image" in log["note"]
        log = C.process({"url": base + "/c.html", "cert_no": "2141438167", "lab": "GIA"}, F.logo(), "sb", "k",
                        uploader=uploader)
        assert log["pdf_url"] == "" and "no PDF provided, online report-check link only" in log["note"] and log["reason"] == "no PDF provided, online report-check link only"
        log = C.process({"url": base + "/c.pdf", "cert_no": "2141438167", "lab": "GIA"}, F.logo(), "sb", "k",
                        uploader=uploader)
        assert log["verification"] == "passed" and log["pdf_url"]
        log = C.process({"url": base + "/missing.pdf", "cert_no": "2141438167", "lab": "GIA"}, F.logo(), "sb",
                        "k", uploader=uploader)
        assert log["pdf_url"] == "" and "HTTP 404" in log["note"]
    finally:
        srv.shutdown()
    # private addresses are refused outside tests
    monkeypatch.setattr(quote_media, "ALLOW_PRIVATE_HOSTS", False)
    C._dl_cache.clear()
    log = C.process({"url": base + "/c.pdf", "cert_no": "2141438167"}, F.logo(), "sb", "k", uploader=uploader)
    assert log["pdf_url"] == "" and "public address" in log["note"]


@pytest.mark.parametrize("pdf,cert_no,lab,words", [
    (F.gia(number_elsewhere=True), "2141438167", "GIA", "unsupported layout"),
    (F.gia(qr_shift=30), "2141438167", "GIA", "QR code"),
    (F.gia(), "2141438167", "HRD", "HRD certificates aren't supported"),
    (F.gia(), "2141438167", "IGI", "looks like a GIA certificate"),
    (F.gia(), "5555555555", "GIA", "doesn't match the stone"),
])
def test_unsupported_left_out(pdf, cert_no, lab, words):
    UP.clear()
    log = run(pdf, cert_no, lab)
    assert log["pdf_url"] == "" and words in log["note"] and not UP


def test_no_certificate_file():
    log = C.process({"url": "", "cert_no": "2141438167"}, F.logo(), "sb", "k", uploader=uploader)
    assert log["source"] == "none" and "no certificate file" in log["note"]


def test_attach_all_never_raises_and_sets_pdf_url():
    stones = [{"cert_type": "GIA"}, {"cert_type": "GIA"}, {"cert_type": "IGI"}]
    jobs = [{"url": "u1", "cert_no": "2141438167", "lab": "GIA"}, None, {"url": "u3", "cert_no": "833689789", "lab": "IGI"}]
    data = {"u1": F.gia(colour=True), "u3": F.jpg()}

    def fetch(u):
        if data[u][:4] != b"%PDF":
            raise C.Skip("Certificate not attached: the certificate is an image (JPG/PNG), not a PDF, so it can't be redacted")
        return data[u]

    def boom(sb, k, b):
        raise RuntimeError("storage down")
    notes, logs = C.attach_all(stones, jobs, F.logo(), "sb", "k", uploader=uploader, fetch=fetch)
    assert stones[0]["pdf_url"].startswith(C.CERT_BASE) and stones[0]["cert_type"] == "GIA Colour"
    assert "pdf_url" not in stones[1] and "pdf_url" not in stones[2]
    assert notes == ["···9789: Certificate not attached: the certificate is an image (JPG/PNG), not a PDF, "
                     "so it can't be redacted"]
    stones = [{}]
    notes, logs = C.attach_all(stones, [{"url": "u1", "cert_no": "2141438167", "lab": "GIA"}], F.logo(), "sb", "k",
                               uploader=boom, fetch=fetch)
    assert "pdf_url" not in stones[0] and "unexpected problem" in notes[0]


def test_client_without_logo_still_verified():
    """Create quote inserts nothing when the client has no logo; the QR area is left blank."""
    UP.clear()
    log = C.process({"url": "u", "cert_no": "2141438167", "lab": "GIA"}, None, "sb", "k",
                    uploader=uploader, fetch=lambda u: F.gia())
    assert log["verification"] == "passed" and log["pdf_url"]


# ── Two more layouts: IGI Photo and GIA Dossier ───────────────────────────────
# tests/data holds copies of two real certificates with every number altered (and the QR code
# and barcode pictures replaced), so the real layouts are tested without the real numbers.
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
REAL_LAYOUTS = [
    # file, stone number, lab, layout, altered number's digits, last 4, how many places hold the number
    ("igi_photo_LG725041893.pdf", "LG725041893", "IGI", "IGI Photo", "725041893", "1893", 8),
    ("gia_dossier_4417250938.pdf", "4417250938", "GIA", "GIA Dossier", "4417250938", "0938", 3),
]
# sha256 of the two real numbers the copies were made from: they must not be anywhere in the copies
ORIGINAL_HASHES = {"1d8cefa8a5476d65b648cbf55970b2734fe5efda525dd805cc0e001f6fa56837",
                   "af85c7ae59a41d87b6236cdc3198c9eec2bb7bc3c8869b4d43366e28f8581fca"}


def has_original(raw):
    import hashlib
    import re as _r
    return any(hashlib.sha256(m.encode()).hexdigest() in ORIGINAL_HASHES
               for n in (9, 10) for m in {raw[i:i + n].decode("latin-1") for i in range(len(raw) - n + 1)}
               if m.isdigit())


def real(name):
    with open(os.path.join(DATA, name), "rb") as f:
        return f.read()


@pytest.mark.parametrize("fn,cert_no,lab,layout,core,last4,places", REAL_LAYOUTS)
def test_new_layout_redacts_and_verifies(fn, cert_no, lab, layout, core, last4, places):
    UP.clear()
    data = real(fn)
    d0 = fitz.open(stream=data, filetype="pdf")
    assert not has_original(data) and not has_original(d0.tobytes(expand=255))
    assert not has_original(d0[0].get_text().encode())                       # the text layer (the GIA copy's fonts hide it in the raw data)
    log = run(data, cert_no, lab)
    assert log["verification"] == "passed", log
    assert log["lab_format"] == layout and log["cert_type"] == lab          # stored type stays the lab
    assert log["pdf_url"].startswith(C.CERT_BASE) and log["redaction"] == "applied"
    out = UP[0]
    text, meta, n_att = _text(out)
    assert core not in re.sub(r"\D", "", text)
    runs = [r for r in re.findall(r"\d+", text) if core.endswith(r) and len(r) > 2]
    assert len(runs) == places and set(runs) == {last4}                      # exactly the last 4, everywhere
    assert not any(v for k, v in meta.items() if k not in ("format", "encryption"))
    assert n_att == 0 and not fitz.open(stream=out).get_xml_metadata().strip()
    raw = fitz.open(stream=out).tobytes(expand=255)
    assert core.encode() not in out and core.encode() not in raw
    # QR area: only the logo
    d = fitz.open(stream=out)
    assert not C.qr_outside(d[0], C._rect(certs.ZONES[layout]["qr"], 3))


def test_igi_photo_was_the_failing_layout_and_old_layouts_do_not_take_it():
    d = fitz.open(stream=real("igi_photo_LG725041893.pdf"), filetype="pdf")
    nums = {"725041893"}
    assert "outside the IGI layout's masked areas" in C.check_layout(d[0], "IGI", nums)   # the original failure
    assert C.check_layout(d[0], "IGI Photo", nums) == ""
    assert C.pick_layout(d[0], "IGI", nums) == ("IGI Photo", "")
    # an old-layout IGI still picks the old layout, even though "IGI Photo" is also a candidate
    o = fitz.open(stream=F.igi(), filetype="pdf")
    assert C.pick_layout(o[0], "IGI", {"833689789"}) == ("IGI", "")
    # (it also fits IGI Photo's places, which is why the original layout is always tried first)
    assert C.check_layout(o[0], "IGI Photo", {"833689789"}) == ""


def test_gia_dossier_not_confused_with_gia_layouts():
    d = fitz.open(stream=real("gia_dossier_4417250938.pdf"), filetype="pdf")
    assert C.detect(d) == ("GIA Dossier", "")
    assert C.detect(d, "GIA") == ("GIA Dossier", "")
    assert "page size" in C.check_layout(d[0], "GIA", {"4417250938"})                     # not the full GIA layout
    assert "page size" in C.check_layout(d[0], "GIA Colour", {"4417250938"})
    assert C.pick_layout(d[0], "GIA Dossier", {"4417250938"}) == ("GIA Dossier", "")
    for old, kind in ((F.gia(), "GIA"), (F.gia(colour=True), "GIA Colour"), (F.igi(), "IGI")):
        od = fitz.open(stream=old, filetype="pdf")
        assert C.detect(od)[0] == kind
        assert C.check_layout(od[0], "GIA Dossier", {"2141438167"}) != ""
    igi = fitz.open(stream=real("igi_photo_LG725041893.pdf"), filetype="pdf")
    assert C.detect(igi) == ("IGI", "")


def test_dossier_report_number_in_every_place():
    """The compact GIA puts the number in three places: top, grading block and the inscription line."""
    d = fitz.open(stream=real("gia_dossier_4417250938.pdf"), filetype="pdf")
    hits = d[0].search_for("4417250938")
    assert len(hits) == 3
    zones = certs.ZONES["GIA Dossier"]
    for h in hits:
        assert any(h.intersects(C._rect(z, 4)) for k, z in zones.items() if k != "qr")
    kinds = {round(h.y0) for h in hits}
    assert len(kinds) == 3


def test_dossier_number_in_an_unexpected_place_is_not_attached():
    UP.clear()
    d = fitz.open(stream=real("gia_dossier_4417250938.pdf"), filetype="pdf")
    d[0].insert_text((30, 350), "Ref 4417250938", fontsize=6)
    log = run(d.tobytes(), "4417250938", "GIA")
    assert log["pdf_url"] == "" and "unsupported layout" in log["note"] and not UP


def test_new_layout_fail_closed_when_digits_left_or_qr_left():
    """A tiny number inside a zone but past the masked part, and a leftover QR, must fail verification."""
    d = fitz.open(stream=real("igi_photo_LG725041893.pdf"), filetype="pdf")
    red = C.strip(certs.redact_pdf(d.tobytes(), "IGI Photo", F.logo()))
    assert C.verify(red, {"725041893"}, "IGI Photo", F.logo()) == []
    # 5 digits showing: too many
    x = fitz.open(stream=red, filetype="pdf")
    x[0].insert_text((362, 42), "41893", fontsize=8)
    fails = C.verify(x.tobytes(), {"725041893"}, "IGI Photo", F.logo())
    assert any("more than the last 4 digits" in f for f in fails)
    # a barcode left in place
    y = fitz.open(stream=red, filetype="pdf")
    y[0].draw_rect(fitz.Rect(820, 47, 900, 58), color=None, fill=(0, 0, 0))
    assert any("barcode area not blank" in f for f in C.verify(y.tobytes(), {"725041893"}, "IGI Photo", F.logo()))
    # a QR left in place
    z = fitz.open(stream=red, filetype="pdf")
    z[0].draw_rect(fitz.Rect(730, 505, 760, 535), color=None, fill=(0, 0, 0))
    assert "QR area" in C.verify(z.tobytes(), {"725041893"}, "IGI Photo", F.logo())


def test_old_layouts_redact_exactly_as_before_after_new_layouts():
    """Same bytes as the pre-change code (git 7927f5d) for the three original layouts."""
    old = subprocess.run(["git", "show", "7927f5d:diamond_redact_app/certs.py"], capture_output=True,
                         text=True, check=True).stdout
    ns = {}
    exec(old, ns)
    no_id = lambda b: b.rsplit(b"trailer", 1)[0]
    with no_barcode_zone():
        for ctype, pdf in (("GIA", F.gia()), ("GIA colour", F.gia(colour=True)), ("IGI", F.igi())):
            t = "GIA Colour" if ctype == "GIA colour" else ctype
            assert no_id(ns["redact_pdf"](pdf, t, F.logo())) == no_id(certs.redact_pdf(pdf, t, F.logo()))
        for t in ("IGI", "GIA", "GIA Colour"):
            assert ns["ZONES"][t] == certs.ZONES[t]
    assert certs.ZONES["IGI"]["barcode"] == certs.ZONES["IGI Photo"]["barcode"]      # the one addition


def test_create_quote_path_uses_new_layouts_and_keeps_old_ones():
    for fn, cert_no, lab, layout, core, last4, places in REAL_LAYOUTS:
        red, used = C.redact_for_quote(real(fn), lab, F.logo())
        assert used == layout
        text, meta, n = _text(red)
        assert core not in re.sub(r"\D", "", text) and n == 0
    red, used = C.redact_for_quote(F.gia(), "GIA", F.logo())
    assert used == "GIA"
    red, used = C.redact_for_quote(F.igi(), "IGI", F.logo())
    assert used == "IGI"
    data = certs.extract_cert_data(real("gia_dossier_4417250938.pdf"), "GIA Dossier")
    assert data["measurements"] == "5.09 - 5.12 x 3.22" and data["fluorescence"] == "Strong Blue"
    assert data["carat"] == "0.52" and data["color"] == "D" and data["clarity"] == "VS2"
    with pytest.raises(C.Skip):                     # a dossier whose number sits somewhere unknown is refused
        d = fitz.open(stream=real("gia_dossier_4417250938.pdf"), filetype="pdf")
        d[0].insert_text((30, 350), "Ref 4417250938", fontsize=6)
        C.redact_for_quote(d.tobytes(), "GIA", F.logo())


# ── Barcodes ──────────────────────────────────────────────────────────────────
def _pix_dark(pdf, zone_key, layout="IGI"):
    d = fitz.open(stream=pdf, filetype="pdf")
    px = d[0].get_pixmap(clip=C._rect(certs.ZONES[layout][zone_key]), dpi=72, colorspace=fitz.csGRAY, alpha=False).samples
    return sum(1 for v in px if v < 200) / len(px)


def test_old_igi_layout_blanks_its_barcode_and_verifies():
    UP.clear()
    pdf = F.add_barcode(F.igi())
    assert _pix_dark(pdf, "barcode") > 0.1                                  # the barcode is there to begin with
    log = run(pdf, "LG833689789", "IGI")
    assert log["verification"] == "passed" and log["lab_format"] == "IGI" and log["cert_type"] == "IGI", log
    assert _pix_dark(UP[0], "barcode") == 0                                 # blank now
    text, meta, n = _text(UP[0])
    assert "833689789" not in re.sub(r"\D", "", text)


def test_old_igi_without_a_barcode_still_redacts_as_before():
    UP.clear()
    log = run(F.igi(), "LG833689789", "IGI")
    assert log["verification"] == "passed" and _pix_dark(UP[0], "barcode") == 0


def test_barcode_left_in_place_fails_verification_on_every_igi_layout():
    for layout, pdf in (("IGI", F.igi()), ("IGI Photo", real("igi_photo_LG725041893.pdf"))):
        red = C.strip(certs.redact_pdf(pdf, layout, F.logo()))
        no = "833689789" if layout == "IGI" else "725041893"
        assert C.verify(red, {no}, layout, F.logo()) == []
        d = fitz.open(stream=red, filetype="pdf")
        d[0].insert_image(fitz.Rect(817.2, 45.1, 913.6, 60.3), stream=F.barcode_png())
        assert any("barcode" in f for f in C.verify(d.tobytes(), {no}, layout, F.logo())), layout


@pytest.mark.parametrize("make,cert_no,lab", [
    (lambda: F.igi(), "LG833689789", "IGI"),
    (lambda: F.gia(), "2141438167", "GIA"),
    (lambda: F.gia(colour=True), "2141438167", "GIA"),
    (lambda: real("gia_dossier_4417250938.pdf"), "4417250938", "GIA"),
])
def test_a_barcode_anywhere_else_is_not_attached(make, cert_no, lab):
    """A barcode-like picture outside the blanked area could still carry the report number: refuse (fail closed)."""
    UP.clear()
    log = run(F.add_barcode(make(), rect=(300, 20, 396, 35)), cert_no, lab)
    assert log["pdf_url"] == "" and "barcode-like picture" in log["note"] and not UP


def test_security_strip_and_logos_are_not_mistaken_for_barcodes():
    for name in ("igi_photo_LG725041893.pdf", "gia_dossier_4417250938.pdf"):
        d = fitz.open(stream=real(name), filetype="pdf")
        for lay in ("IGI Photo", "GIA Dossier"):
            if certs.LAYOUT_META[lay]["lab"] == ("IGI" if "igi" in name else "GIA"):
                assert certs.barcode_outside(d[0], lay) == ""
