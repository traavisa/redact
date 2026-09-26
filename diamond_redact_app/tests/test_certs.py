"""Part 2: certificates for Live Search stones — redaction, verification, fail-closed."""
import http.server
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
    """certs.redact_pdf is byte-for-byte the Create quote code it was moved from."""
    old = subprocess.run(["git", "show", "origin/main:diamond_redact_app/app.py"], capture_output=True,
                         text=True, check=True).stdout
    zones_src = old[old.index("CERT_ZONES = {"):old.index("PADDING       = 1.5")]
    fn_src = old[old.index("def redact_pdf("):old.index("import re as _re")]
    ns = {"fitz": fitz, "io": __import__("io"), "IGI_B64": "", "GIA_B64": ""}
    exec(zones_src + "PADDING = 1.5\n" + fn_src, ns)
    no_id = lambda b: b.rsplit(b"trailer", 1)[0]      # the trailer holds the file ID, random per save
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
        assert log["pdf_url"] == "" and "image (JPG/PNG), not a PDF" in log["note"]
        log = C.process({"url": base + "/c.html", "cert_no": "2141438167", "lab": "GIA"}, F.logo(), "sb", "k",
                        uploader=uploader)
        assert log["pdf_url"] == "" and "isn't a PDF file" in log["note"]
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
