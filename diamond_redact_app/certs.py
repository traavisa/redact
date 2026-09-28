"""Certificate redaction and grading-data extraction for IGI, GIA, GIA Colour, IGI Photo and GIA Dossier PDFs.

Used by Create quote (uploaded certificates) and by Live Search / direct lookup (certificates
fetched from the stone data, see cert_attach.py). Masks all but the last 4 digits of the
report and inscription numbers and replaces the QR code with a logo.
"""
import io
import re as _re

import fitz

# Number and QR positions per certificate layout (PDF points).
ZONES = {
    "IGI": {
    "num_top_centre":   {"x0":360.8,"y0":34.1, "x1":420.9,"y1":43.6, "mask_ratio":0.55},
    "num_left":         {"x0":181.8,"y0":150.1,"x1":232.5,"y1":158.1,"mask_ratio":0.55},
    "num_left_insc":    {"x0":183.7,"y0":371.5,"x1":233.4,"y1":379.5,"mask_ratio":0.55},
    "num_right_top":    {"x0":940.3,"y0":75.2, "x1":981.9,"y1":81.2, "mask_ratio":0.55},
    "num_right_insc":   {"x0":933.0,"y0":355.9,"x1":981.9,"y1":361.9,"mask_ratio":0.55},
    "num_vert_report":  {"x0":810.6,"y0":535.8,"x1":818.6,"y1":588.6,"mask_ratio":0.55,"vertical":True},
    "num_vert_insc":    {"x0":919.5,"y0":509.4,"x1":927.4,"y1":540.1,"mask_ratio":0.55,"vertical":True},
    "num_diamond_high": {"x0":630.0,"y0":150.0,"x1":710.0,"y1":168.0,"mask_ratio":0.55},
    "num_diamond_low":  {"x0":622.0,"y0":308.0,"x1":700.0,"y1":326.0,"mask_ratio":0.55},
    "qr":               {"x0":725.1,"y0":500.9,"x1":768.3,"y1":544.0},
    },
    "GIA": {
    "gia1":        {"x0":367.7,"y0":47.0, "x1":431.5,"y1":59.0, "mask_ratio":0.60},
    "gia2":        {"x0":192.8,"y0":117.9,"x1":241.6,"y1":126.9,"mask_ratio":0.60},
    "inscription": {"x0":95.1, "y0":339.4,"x1":143.0,"y1":348.4,"mask_ratio":0.60},
    "qr":          {"x0":698.0,"y0":504.2,"x1":752.0,"y1":558.2},
    },
    "GIA Colour": {
    "gia1":        {"x0":367.7,"y0":45.0, "x1":431.5,"y1":57.0, "mask_ratio":0.60},
    "gia2":        {"x0":192.8,"y0":137.2,"x1":241.6,"y1":146.2,"mask_ratio":0.60},
    "inscription": {"x0":95.1, "y0":446.9,"x1":143.0,"y1":455.9,"mask_ratio":0.60},
    "qr":          {"x0":698.1,"y0":507.1,"x1":752.1,"y1":561.1},
    },
}

# Two further layouts (added after the three above, which are untouched):
#   "IGI Photo"   — the IGI report whose diamond photo carries the inscription number mid-photo
#                   ("Sample Image Used"); same page and other number positions as "IGI".
#   "GIA Dossier" — the compact GIA Natural Diamond Dossier (509 x 360 pt): the report number at
#                   the top, in the grading block and in the laser inscription line.
# Their zones mark "last4" places: every run of 7+ digits found there is masked up to the last
# 4 digits, worked out from the glyph positions (not a fixed share of the box). Their "meta"
# entry gives the lab they belong to and the exact page size they are made for.
_IGI_PLACES = ("num_top_centre", "num_left", "num_left_insc", "num_right_top", "num_right_insc",
               "num_vert_report", "num_vert_insc")
ZONES["IGI Photo"] = {k: dict(ZONES["IGI"][k], last4=True) for k in _IGI_PLACES}
ZONES["IGI Photo"]["num_diamond_photo"] = {"x0":640.0,"y0":184.0,"x1":700.0,"y1":196.0,"last4":True}
ZONES["IGI Photo"]["qr"] = dict(ZONES["IGI"]["qr"])
# The barcode above the date encodes the report number: blanked (its area must come out white).
ZONES["IGI Photo"]["barcode"] = {"x0":817.2,"y0":45.1,"x1":913.6,"y1":60.3,"pad":0.5,"blank":True}
ZONES["GIA Dossier"] = {
    "num_head":   {"x0":350.2,"y0":36.1, "x1":414.1,"y1":49.6, "last4":True},
    "num_report": {"x0":189.3,"y0":92.0, "x1":229.9,"y1":100.5,"last4":True},
    "num_insc":   {"x0":80.7, "y0":252.4,"x1":120.6,"y1":260.8,"last4":True},
    "qr":         {"x0":441.2,"y0":283.85,"x1":495.2,"y1":337.85,"pad":0.0},
}
LAYOUT_META = {
    "IGI Photo":   {"lab": "IGI", "page": (1008.0, 612.0)},
    "GIA Dossier": {"lab": "GIA", "page": (509.05, 360.0)},
}
PAGE_TOL = 3.0
KEEP_DIGITS = 4
MIN_MASKED_RUN = 7      # a digit run shorter than this isn't a report / inscription number

PADDING       = 1.5


def lab_of(cert_type):
    """The lab a layout belongs to: 'IGI', 'GIA' or 'GIA Colour' (what quotes store as cert_type)."""
    return LAYOUT_META.get(cert_type, {}).get("lab", cert_type)


def page_fits(page, cert_type):
    """True when the page is the exact size a last4 layout was made for (always True for the older layouts)."""
    want = LAYOUT_META.get(cert_type, {}).get("page")
    if not want:
        return True
    return abs(page.rect.width - want[0]) <= PAGE_TOL and abs(page.rect.height - want[1]) <= PAGE_TOL


def zone_rect(z, pad=None):
    p = z.get("pad", PADDING) if pad is None else pad
    return fitz.Rect(z["x0"] - p, z["y0"] - p, z["x1"] + p, z["y1"] + p)


def last4_rects(page, area, keep=KEEP_DIGITS):
    """Rectangles that hide every digit but the last `keep` of each number (7+ digits, plus an
    'LG' stuck to it) whose glyphs touch `area`. Positions come from the glyphs themselves."""
    rects = []
    for blk in page.get_text("rawdict")["blocks"]:
        for line in blk.get("lines", []):
            dx, dy = line.get("dir", (1, 0))
            for span in line["spans"]:
                chars = span["chars"]
                text = "".join(c["c"] for c in chars)
                for m in _re.finditer(r"\d{%d,}" % MIN_MASKED_RUN, text):
                    a, b = m.start(), m.end()
                    if not fitz.Rect(*chars[a]["bbox"]).intersects(area) and \
                       not any(fitz.Rect(*c["bbox"]).intersects(area) for c in chars[a:b]):
                        continue
                    if text[max(0, a - 2):a] == "LG":
                        a -= 2
                    hide, kept = chars[a:b - keep], chars[b - keep]
                    r = fitz.Rect(*hide[0]["bbox"])
                    for c in hide[1:]:
                        r |= fitz.Rect(*c["bbox"])
                    k = fitz.Rect(*kept["bbox"])
                    if abs(dx) >= abs(dy):                     # horizontal
                        if dx > 0: r.x1 = min(r.x1, k.x0 - 0.1)
                        else:      r.x0 = max(r.x0, k.x1 + 0.1)
                    else:                                      # vertical
                        if dy < 0: r.y0 = max(r.y0, k.y1 + 0.1)
                        else:      r.y1 = min(r.y1, k.y0 - 0.1)
                    if not r.is_empty:
                        rects.append(r)
    return rects


def redact_pdf(file_bytes, cert_type, logo_img):
    zones = ZONES[cert_type]
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    for page in doc:
        for key, z in zones.items():
            pad=z.get("pad", PADDING)
            x0=z["x0"]-pad; y0=z["y0"]-pad
            x1=z["x1"]+pad; y1=z["y1"]+pad
            if z.get("last4"):
                for r in last4_rects(page, fitz.Rect(x0,y0,x1,y1)):
                    page.add_redact_annot(r,fill=(1,1,1)); page.apply_redactions()
            elif z.get("blank"):
                page.add_redact_annot(fitz.Rect(x0,y0,x1,y1),fill=(1,1,1)); page.apply_redactions()
            elif key=="qr":
                rect=fitz.Rect(x0,y0,x1,y1)
                page.add_redact_annot(rect,fill=(1,1,1)); page.apply_redactions()
                if logo_img:
                    buf=io.BytesIO(); logo_img.save(buf,format="PNG"); buf.seek(0)
                    page.insert_image(rect,stream=buf.read())
            elif z.get("vertical"):
                r=z.get("mask_ratio",0.55)
                page.add_redact_annot(fitz.Rect(x0,y0,x1,y0+(y1-y0)*r),fill=(1,1,1))
                page.apply_redactions()
            else:
                r=z.get("mask_ratio",0.60)
                page.add_redact_annot(fitz.Rect(x0,y0,x0+(x1-x0)*r,y1),fill=(1,1,1))
                page.apply_redactions()
    out=io.BytesIO()
    doc.save(out,garbage=4,deflate=True,clean=True)
    doc.close(); out.seek(0)
    return out.read()


def extract_cert_data_gia(text):
    def find(pattern, default=""):
        m = _re.search(pattern, text, _re.IGNORECASE)
        return m.group(1).strip() if m else default

    shape_full  = find(r"Shape and Cutting Style\s*\.+\s*(.+)")
    shape       = _re.match(r"^(\w+)", shape_full).group(1) if shape_full else ""
    cut         = find(r"Cut Grade\s*\.+\s*(\S+)")
    carat       = find(r"Carat Weight\s*\.+\s*([\d.]+)")
    color       = find(r"Color Grade\s*\.+\s*([A-Z])\b")
    clarity     = find(r"Clarity Grade\s*\.+\s*(\S+)")
    meas        = find(r"Measurements\s*\.+\s*([\d.]+ x [\d.]+ x [\d.]+)")
    polish      = find(r"Polish\s*\.+\s*(\S+)")
    symmetry    = find(r"Symmetry\s*\.+\s*(\S+)")
    fluor       = find(r"Fluorescence\s*\.+\s*(\S+)")
    ratio = ""
    if meas:
        parts = _re.findall(r"[\d.]+", meas)
        if len(parts) >= 2:
            try: ratio = f"{float(parts[0]) / float(parts[1]):.2f}"
            except: pass
    return dict(shape=shape, cut=cut, carat=carat, color=color, clarity=clarity,
                measurements=meas, ratio=ratio, polish=polish, symmetry=symmetry,
                fluorescence=fluor)


def extract_cert_data_gia_dossier(text):
    """The compact Diamond Dossier: same labels as the full GIA report, but the measurements can
    be a range ("5.09 - 5.12 x 3.22 mm") and the fluorescence can be two words ("Strong Blue")."""
    out = extract_cert_data_gia(text)
    m = _re.search(r"Measurements\s*\.+\s*([\d.]+)(?:\s*-\s*([\d.]+))?\s*x\s*([\d.]+)", text, _re.IGNORECASE)
    if m:
        lo, hi, depth = m.group(1), m.group(2), m.group(3)
        out["measurements"] = f"{lo} - {hi} x {depth}" if hi else f"{lo} x {depth}"
        try:
            if hi:
                a_, b_ = sorted((float(lo), float(hi)))
                out["ratio"] = f"{b_ / a_:.2f}"
        except Exception:
            pass
    m = _re.search(r"Fluorescence\s*\.+\s*(.+)", text, _re.IGNORECASE)
    if m:
        out["fluorescence"] = m.group(1).strip()
    return out


def extract_cert_data_gia_colour(text):
    def find(pattern, default=""):
        m = _re.search(pattern, text, _re.IGNORECASE)
        return m.group(1).strip() if m else default

    shape_full       = find(r"Shape and Cutting Style\s*\.+\s*(.+)")
    shape            = _re.match(r"^(\w+)", shape_full).group(1) if shape_full else ""
    carat            = find(r"Carat Weight\s*\.+\s*([\d.]+)")
    color            = find(r"Color Grade\s*\.+\s*(.+)")
    color_origin     = find(r"Color Origin\s*\.+\s*(\S+)")
    color_dist       = find(r"Color Distribution\s*\.+\s*(\S+)")
    clarity          = find(r"Clarity Grade\s*\.+\s*(\S+)")
    meas             = find(r"Measurements\s*\.+\s*([\d.]+ x [\d.]+ x [\d.]+)")
    polish           = find(r"Polish\s*\.+\s*(\S+)")
    symmetry         = find(r"Symmetry\s*\.+\s*(.+?)(?:\n|$)")
    fluor            = find(r"Fluorescence\s*\.+\s*(\S+)")
    ratio = ""
    if meas:
        parts = _re.findall(r"[\d.]+", meas)
        if len(parts) >= 2:
            try: ratio = f"{float(parts[0]) / float(parts[1]):.2f}"
            except: pass
    return dict(shape=shape, cut="", carat=carat, color=color,
                color_origin=color_origin, color_distribution=color_dist,
                clarity=clarity, measurements=meas, ratio=ratio,
                polish=polish, symmetry=symmetry, fluorescence=fluor)


def extract_cert_data_igi(text):
    def find(pattern, default=""):
        m = _re.search(pattern, text, _re.IGNORECASE)
        return m.group(1).strip() if m else default

    shape_full   = find(r"Shape and Cutting Style\n(.+)")
    shape        = _re.match(r"^(\w+)", shape_full).group(1).title() if shape_full else ""
    cut          = find(r"Cut Grade\n(\S+)")
    carat        = find(r"Carat Weight\n([\d.]+)")
    color        = find(r"Color Grade\n\s*([A-Z])\b")
    clarity_raw  = find(r"Clarity Grade\n(\S+(?:\s+\d)?)")
    clarity      = clarity_raw.replace(" ", "")
    meas_raw     = find(r"Measurements\n([\d.]+ X [\d.]+ X [\d.]+)")
    meas         = meas_raw.replace(" X ", " x ") if meas_raw else ""
    polish       = find(r"Polish\n(\S+)").title()
    symmetry     = find(r"Symmetry\n(\S+)").title()
    fluor        = find(r"Fluorescence\n(\S+)").title()
    ratio = ""
    if meas:
        parts = _re.findall(r"[\d.]+", meas)
        if len(parts) >= 2:
            try: ratio = f"{float(parts[0]) / float(parts[1]):.2f}"
            except: pass
    return dict(shape=shape, cut=cut, carat=carat, color=color, clarity=clarity,
                measurements=meas, ratio=ratio, polish=polish, symmetry=symmetry,
                fluorescence=fluor)


def extract_cert_data(file_bytes, cert_type):
    """Router — extracts grading data from GIA, GIA Colour, or IGI certificate PDFs."""
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        text = doc[0].get_text()
        doc.close()
    except:
        return {}

    if cert_type in ("IGI", "IGI Photo"):
        raw = extract_cert_data_igi(text)
    elif cert_type == "GIA Dossier":
        raw = extract_cert_data_gia_dossier(text)
    elif cert_type == "GIA Colour":
        raw = extract_cert_data_gia_colour(text)
    else:
        raw = extract_cert_data_gia(text)

    return {k: v for k, v in raw.items() if v}
