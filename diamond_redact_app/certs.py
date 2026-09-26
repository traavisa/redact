"""Certificate redaction and grading-data extraction for IGI, GIA and GIA Colour PDFs.

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

PADDING       = 1.5


def redact_pdf(file_bytes, cert_type, logo_img):
    zones = ZONES[cert_type]
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    for page in doc:
        for key, z in zones.items():
            x0=z["x0"]-PADDING; y0=z["y0"]-PADDING
            x1=z["x1"]+PADDING; y1=z["y1"]+PADDING
            if key=="qr":
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

    if cert_type == "IGI":
        raw = extract_cert_data_igi(text)
    elif cert_type == "GIA Colour":
        raw = extract_cert_data_gia_colour(text)
    else:
        raw = extract_cert_data_gia(text)

    return {k: v for k, v in raw.items() if v}
