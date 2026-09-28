"""Certificates for Live Search and direct-lookup stones, attached when a quote is saved.

For each stone with a certificate file in the stone data:
  1. download it server-side (public addresses only, size-limited);
  2. only a PDF is used: an image (JPG/PNG) or anything else is left out with a note;
  3. detect the lab/format (IGI, GIA, GIA Colour, GIA Dossier) from its text and check it against
     the layout the redaction zones were made for (an IGI report is tried against the original
     IGI layout first, then the IGI Photo layout; a layout is only used when every place the
     full number appears lies inside its masked areas);
  4. redact with certs.redact_pdf (the same code as Create quote: all but the last 4 digits
     of the report and inscription numbers masked, the QR code replaced by the selected
     client's logo — or left blank when the client has no logo — saved with garbage=4,
     deflate=True, clean=True), then strip metadata;
  5. FAIL CLOSED: verify that the full report and inscription numbers appear nowhere (text
     layer, metadata, annotations, form fields, attachments, raw streams) and that the QR
     area shows only the logo. Any failure: not attached;
  6. store the redacted PDF in the "certificates" bucket under a random name, served from
     our own domain (/certs/<random>.pdf).

Never raises and never blocks saving a quote: every problem becomes a short note.
Log lines ("[cert] …") carry the last 4 digits only.
"""
import io
import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import fitz
import requests

import certs
import quote_media as qm

BUCKET = "certificates"
CERT_BASE = "https://quote.alldiamondeverything.com/certs/"
MAX_BYTES = 15 * 1024 * 1024
TIMEOUT = (10, 30)
DEADLINE = 45                        # seconds for one whole download, however slowly it trickles in
NOT_VERIFIED = "Certificate not attached: redaction couldn't be verified"


class Skip(Exception):
    """Certificate left out. str(e) is the note shown to the user."""


# ── 1-2. Download ─────────────────────────────────────────────────────────────
def download(url):
    """Returns the PDF bytes, or raises Skip with the reason."""
    url = str(url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise Skip("Certificate not attached: the certificate link isn't a web address")
    try:
        if not qm._public_host(url):
            raise Skip("Certificate not attached: the certificate link isn't a public address")
    except qm.Broken:
        raise Skip("Certificate not attached: the certificate link doesn't work (host not found)")
    try:
        r = requests.get(url, stream=True, timeout=TIMEOUT, allow_redirects=True,
                         headers={"User-Agent": qm.UA, "Accept": "application/pdf,*/*"})
    except requests.RequestException as e:
        raise Skip(f"Certificate not attached: couldn't download it ({type(e).__name__})")
    with r:
        if r.status_code >= 400:
            raise Skip(f"Certificate not attached: download failed (HTTP {r.status_code})")
        buf = io.BytesIO()
        start = time.monotonic()
        try:
            for chunk in r.iter_content(256 * 1024):
                buf.write(chunk)
                if buf.tell() > MAX_BYTES:
                    raise Skip("Certificate not attached: the file is too large")
                if time.monotonic() - start > DEADLINE:
                    raise Skip("Certificate not attached: the download took too long")
        except requests.RequestException as e:
            raise Skip(f"Certificate not attached: download interrupted ({type(e).__name__})")
    data = buf.getvalue()
    head = data[:1024]
    if b"%PDF-" in head:
        return data
    if qm._sniff(data[:16]) == "image":
        raise Skip("Certificate not attached: the certificate is an image (JPG/PNG), not a PDF, "
                   "so it can't be redacted")
    raise Skip("Certificate not attached: the certificate link isn't a PDF file")


# ── 3. Lab / format ───────────────────────────────────────────────────────────
def digits(s):
    return re.sub(r"\D", "", str(s or ""))


def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def detect(doc, lab_hint=""):
    """(cert_type, reason). cert_type is 'IGI', 'GIA', 'GIA Colour' or 'GIA Dossier', or None with a reason."""
    if doc.needs_pass or doc.is_encrypted:
        return None, "the PDF is password-protected"
    if doc.page_count < 1:
        return None, "the PDF has no pages"
    text = doc[0].get_text()
    up = " ".join(text.upper().split())
    if len(up) < 40:
        return None, "the PDF has no text layer (a scan), so redaction can't be verified"
    is_igi = "INTERNATIONAL GEMOLOGICAL INSTITUTE" in up or re.search(r"\bIGI\b", up) is not None
    is_gia = "GEMOLOGICAL INSTITUTE OF AMERICA" in up or re.search(r"\bGIA\b", up) is not None
    hint = str(lab_hint or "").strip().upper()
    if hint and hint not in ("GIA", "IGI"):
        return None, f"{hint} certificates aren't supported (IGI and GIA only)"
    if is_igi and (hint == "IGI" or not is_gia):
        lab = "IGI"
    elif is_gia and (hint == "GIA" or not is_igi):
        lab = "GIA"
    else:
        return None, "the lab isn't IGI or GIA"
    if hint and hint != lab:
        return None, f"the PDF looks like a {lab} certificate but the stone says {hint}"
    if lab == "GIA" and "DIAMOND DOSSIER" in up:
        return "GIA Dossier", ""
    if lab == "GIA" and ("COLOR ORIGIN" in up or "COLORED DIAMOND" in up or "COLOURED DIAMOND" in up):
        return "GIA Colour", ""
    return lab, ""


def _rect(z, pad=None):
    """A zone as a rectangle; the zone's own padding unless one is given."""
    return certs.zone_rect(z, pad)


def pick_layout(page, cert_type, numbers):
    """(layout, reason). The layout whose masked areas cover every place the numbers appear:
    an IGI report is tried against 'IGI' first (so existing IGI reports are always handled as
    before), then 'IGI Photo'. Nothing fits: the first candidate and why it doesn't."""
    candidates = ["IGI", "IGI Photo"] if cert_type == "IGI" else [cert_type]
    first_why = ""
    for c in candidates:
        why = check_layout(page, c, numbers)
        if not why:
            return c, ""
        first_why = first_why or why
    return candidates[0], first_why


def sensitive_numbers(text, cert_no):
    """Full report / inscription numbers that must not survive redaction."""
    up = str(text or "").upper()
    core = digits(cert_no)
    found = set()
    if core:
        found.add(core)
    for m in re.finditer(r"\bLG ?(\d{6,})", up):
        found.add(m.group(1))
    for m in re.finditer(r"(?:REPORT\s*(?:NUMBER|NO\.?|#)|INSCRIPTION[^:\n]{0,30}:?)\s*[:#]?\s*(?:GIA|IGI|LG)?\s*(\d{6,})", up):
        found.add(m.group(1))
    return {n for n in found if len(n) >= 6}


def check_layout(page, cert_type, numbers):
    """Every place a full number appears must be inside one of the layout's number zones,
    and the page must be big enough for the layout. Returns '' or the reason it isn't."""
    zones = certs.ZONES[cert_type]
    need_w = max(z["x1"] for z in zones.values())
    need_h = max(z["y1"] for z in zones.values())
    if cert_type in certs.LAYOUT_META:
        if not certs.page_fits(page, cert_type):
            return (f"page size {page.rect.width:.0f}×{page.rect.height:.0f} doesn't match the {cert_type} layout")
    elif page.rect.width < need_w or page.rect.height < need_h or page.rect.width > need_w * 1.6:
        return (f"page size {page.rect.width:.0f}×{page.rect.height:.0f} doesn't match the {cert_type} layout")
    num_rects = [_rect(z, 4) for k, z in zones.items() if k != "qr" and not z.get("blank")]
    seen = 0
    for n in numbers:
        for hit in page.search_for(n):
            seen += 1
            if not any(hit.intersects(r) for r in num_rects):
                return f"a report/inscription number is outside the {cert_type} layout's masked areas"
    if not seen:
        return "the report number isn't in the PDF's text, so the redaction can't be checked"
    return qr_outside(page, _rect(zones["qr"], zones["qr"].get("pad", certs.PADDING) + 1.5)) \
        or certs.barcode_outside(page, cert_type)


def qr_outside(page, qr):
    """'' when every QR-like mark on the page lies inside the layout's QR area, else why not.
    QR-like: small images touching the area, and clusters of tiny filled squares (vector QR)."""
    for info in page.get_image_info():
        b = fitz.Rect(info["bbox"])
        if b.intersects(qr) and b.width < 150 and b.height < 150 and not qr.contains(b):
            return "the QR code isn't where the layout expects it"
    cells = []
    for d in page.get_drawings():
        r = d.get("rect")
        if d.get("fill") is not None and r is not None and 0 < r.width <= 6 and 0 < r.height <= 6:
            cells.append(fitz.Rect(r))
    near = [r for r in cells if r.intersects(fitz.Rect(qr.x0 - 30, qr.y0 - 30, qr.x1 + 30, qr.y1 + 30))]
    if any(not qr.contains(r) for r in near):
        return "the QR code isn't where the layout expects it"
    if len(cells) - len(near) >= 50:
        return "there is a QR-like code outside the layout's QR area"
    return ""


# ── 4. Strip ──────────────────────────────────────────────────────────────────
def strip(pdf_bytes):
    """Metadata, XMP, attachments, annotations and form fields removed; same save options."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        doc.set_metadata({})
        doc.del_xml_metadata()
        for name in list(doc.embfile_names()):
            doc.embfile_del(name)
        for page in doc:
            for a in list(page.annots() or []):
                page.delete_annot(a)
            for w in list(page.widgets() or []):
                page.delete_widget(w)
        out = io.BytesIO()
        doc.save(out, garbage=4, deflate=True, clean=True)
        return out.getvalue()
    finally:
        doc.close()


# ── 5. Verify ─────────────────────────────────────────────────────────────────
def _qr_reference(page_rect, qr_rect, logo_img):
    ref = fitz.open()
    p = ref.new_page(width=page_rect.width, height=page_rect.height)
    p.draw_rect(qr_rect, color=None, fill=(1, 1, 1))
    if logo_img is not None:
        buf = io.BytesIO()
        logo_img.save(buf, format="PNG")
        p.insert_image(qr_rect, stream=buf.getvalue())
    return ref


def _pixels(page, clip):
    return page.get_pixmap(clip=clip, dpi=72, alpha=False).samples


def too_many_digits(text, numbers):
    """True when a digit run in `text` shows more than the last KEEP_DIGITS digits of a number
    (it is a piece of the number longer than that). Used for the last-4 layouts."""
    for run in re.findall(r"\d+", str(text or "")):
        if len(run) > certs.KEEP_DIGITS and any(run in n for n in numbers):
            return True
    return False


def verify(pdf_bytes, numbers, cert_type, logo_img):
    """[] when the redacted PDF is safe, else the list of failed checks."""
    fails = []
    variants = set()
    for n in numbers:
        variants |= {n, "LG" + n}
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return ["redacted PDF can't be opened"]
    try:
        def leaks(text):
            t = _norm(text)
            d = digits(text)
            return any(v in t for v in variants) or any(n in d for n in numbers)

        last4 = cert_type in certs.LAYOUT_META
        for i, page in enumerate(doc):
            if leaks(page.get_text()):
                fails.append(f"text layer (page {i + 1})")
            elif last4 and too_many_digits(page.get_text(), numbers):
                fails.append(f"more than the last {certs.KEEP_DIGITS} digits shown (page {i + 1})")
            for a in page.annots() or []:
                if leaks(json.dumps(a.info)):
                    fails.append(f"annotation (page {i + 1})")
            for w in page.widgets() or []:
                if leaks(f"{w.field_name} {w.field_value} {w.field_label}"):
                    fails.append(f"form field (page {i + 1})")
        if leaks(json.dumps(doc.metadata or {})):
            fails.append("metadata")
        if any(str(v or "").strip() for k, v in (doc.metadata or {}).items() if k not in ("format", "encryption")):
            fails.append("metadata not stripped")
        if (doc.get_xml_metadata() or "").strip():
            fails.append("XMP metadata not stripped")
        if doc.embfile_count():
            fails.append("attachments present")
        raw = doc.tobytes(expand=255, garbage=0).upper()
        if any(v.encode() in raw for v in variants):
            fails.append("raw PDF data")
        # Any barcode-like picture must be inside a blanked area (and blanked areas must come out white)
        if certs.barcode_outside(doc[0], cert_type):
            fails.append("barcode")
        # Blanked areas (a barcode that encodes the number) must come out white
        for key, z in certs.ZONES[cert_type].items():
            if z.get("blank"):
                px = _pixels(doc[0], _rect(z))
                if sum(1 for v in px if v < 230) > 0.01 * len(px):
                    fails.append(f"{key} area not blank")
        # The QR area must show only the logo: compare with a clean page carrying just the logo
        page = doc[0]
        qr = _rect(certs.ZONES[cert_type]["qr"])
        ref = _qr_reference(page.rect, qr, logo_img)
        try:
            a, b = _pixels(page, qr), _pixels(ref[0], qr)
            if len(a) != len(b):
                fails.append("QR area")
            else:
                diff = sum(1 for x, y in zip(a, b) if abs(x - y) > 40)
                if diff > 0.02 * len(a):
                    fails.append("QR area")
        finally:
            ref.close()
    finally:
        doc.close()
    return fails


# ── 6. Store ──────────────────────────────────────────────────────────────────
def upload(sb_url, key, pdf_bytes):
    name = f"{uuid.uuid4().hex}.pdf"
    r = requests.post(f"{sb_url}/storage/v1/object/{BUCKET}/{name}",
                      headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/pdf",
                               "Cache-Control": "max-age=31536000", "x-upsert": "false"},
                      data=pdf_bytes, timeout=(10, 60))
    if r.status_code not in (200, 201):
        raise Skip(f"Certificate not attached: storing it failed (HTTP {r.status_code})")
    return CERT_BASE + name


# ── Create quote: one uploaded certificate ────────────────────────────────────
def resolve_layout(data, selected):
    """(layout, numbers) for a certificate uploaded in Create quote under the card `selected`
    ('IGI', 'GIA' or 'GIA Colour'). The selected type's own layout is used exactly as before,
    unless the file doesn't fit it and does fit one of the newer layouts (IGI Photo for an IGI
    card, GIA Dossier for a GIA card) — then that one, with the numbers found in its text."""
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return selected, set()
    try:
        if doc.needs_pass or doc.is_encrypted or doc.page_count < 1:
            return selected, set()
        text = doc[0].get_text()
        numbers = sensitive_numbers(text, "") | set(re.findall(r"\d{8,}", text))
        up = " ".join(text.upper().split())
        if not numbers:
            return selected, numbers
        if selected == "IGI":
            layout, why = pick_layout(doc[0], "IGI", numbers)
            return (layout if not why else "IGI"), numbers
        if selected in ("GIA", "GIA Colour") and "DIAMOND DOSSIER" in up:
            why = check_layout(doc[0], "GIA Dossier", numbers)
            if not why:
                return "GIA Dossier", numbers
            # a Diamond Dossier is never redacted with the full-size GIA zones
            raise Skip(f"unsupported GIA Diamond Dossier layout ({why})")
        return selected, numbers
    finally:
        doc.close()


def redact_for_quote(data, selected, logo_img):
    """(redacted_pdf, layout). The three original layouts redact exactly as they always did.
    A newer layout is also stripped of metadata and verified (fail closed): raises Skip with
    the reason when the numbers or the QR code weren't safely removed."""
    layout, numbers = resolve_layout(data, selected)
    if layout not in certs.LAYOUT_META:
        return certs.redact_pdf(data, selected, logo_img), selected
    red = strip(certs.redact_pdf(data, layout, logo_img))
    fails = verify(red, numbers, layout, logo_img)
    if fails:
        raise Skip("redaction couldn't be verified (" + ", ".join(fails) + ")")
    return red, layout


# ── One stone ─────────────────────────────────────────────────────────────────
def process(job, logo_img, sb_url, key, uploader=None, fetch=None):
    """job: {"url", "cert_no", "lab"}. Returns a log dict: source, lab_format, redaction,
    verification, pdf_url ('' when not attached), cert_type, note."""
    uploader, fetch = uploader or upload, fetch or download
    last4 = digits((job or {}).get("cert_no"))[-4:]
    log = {"stone": f"···{last4}" if last4 else "?", "source": "none", "lab_format": "-",
           "redaction": "not applied", "verification": "-", "pdf_url": "", "cert_type": "", "note": ""}
    try:
        if not (job or {}).get("url"):
            raise Skip("Certificate not attached: no certificate file in the stone data")
        log["source"] = "found"
        data = fetch(job["url"])
        log["source"] = "found (PDF)"
        try:
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception:
            raise Skip("Certificate not attached: the PDF can't be read")
        try:
            ctype, why = detect(doc, job.get("lab"))
            if not ctype:
                log["lab_format"] = "unsupported"
                raise Skip(f"Certificate not attached: unsupported certificate ({why})")
            log["lab_format"] = ctype
            text = "\n".join(p.get_text() for p in doc)
            numbers = sensitive_numbers(text, job.get("cert_no"))
            core = digits(job.get("cert_no"))
            if core and core not in digits(text):
                raise Skip("Certificate not attached: the certificate's number doesn't match the stone")
            if not numbers:
                raise Skip("Certificate not attached: no report number found to redact")
            layout, why = pick_layout(doc[0], ctype, numbers)
            if why:
                log["lab_format"] = f"{ctype} (unsupported layout)"
                raise Skip(f"Certificate not attached: unsupported layout ({why})")
            log["lab_format"] = layout
        finally:
            doc.close()
        red = strip(certs.redact_pdf(data, layout, logo_img))
        log["redaction"] = "applied"
        fails = verify(red, numbers, layout, logo_img)
        log["verification"] = "passed" if not fails else "FAILED: " + ", ".join(fails)
        if fails:
            raise Skip(NOT_VERIFIED)
        log["pdf_url"] = uploader(sb_url, key, red)
        log["cert_type"] = certs.lab_of(layout)
    except Skip as e:
        log["note"] = str(e)
    except Exception as e:
        log["note"] = f"Certificate not attached: unexpected problem ({type(e).__name__})"
        if log["redaction"] == "applied" and log["verification"] == "-":
            log["verification"] = "FAILED: error"
    return log


def attach_all(stones, jobs, logo_img, sb_url, key, workers=4, **kw):
    """Processes certificate jobs (one per stone; None = not a Live Search stone) and sets
    pdf_url on the stones that pass. Returns (notes, logs). Never raises."""
    todo = [(i, j) for i, j in enumerate(jobs) if j is not None]
    if not todo:
        return [], []
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            logs = list(ex.map(lambda t: process(t[1], logo_img, sb_url, key, **kw), todo))
    except Exception as e:
        logs = [{"stone": "?", "note": f"Certificate not attached: unexpected problem ({type(e).__name__})",
                 "pdf_url": ""} for _ in todo]
    notes = []
    for (i, _), lg in zip(todo, logs):
        if lg.get("pdf_url"):
            stones[i]["pdf_url"] = lg["pdf_url"]
            if lg.get("cert_type"):
                stones[i]["cert_type"] = lg["cert_type"]
        elif lg.get("note"):
            notes.append(f"{lg['stone']}: {lg['note']}")
        try:
            print("[cert] " + json.dumps({k: v for k, v in lg.items() if k != "pdf_url"}
                                         | {"attached": bool(lg.get("pdf_url"))}), flush=True)
        except Exception:
            pass
    return notes, logs
