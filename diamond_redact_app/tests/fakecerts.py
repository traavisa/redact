"""Synthetic certificates laid out like the real GIA / IGI PDFs the redaction zones were made for."""
import io

import fitz
from PIL import Image


def _qr(page, rect, cells=21):
    """A QR-like block of small black squares filling `rect`."""
    s = rect.width / cells
    for i in range(cells):
        for j in range(cells):
            if (i * 7 + j * 3 + i * j) % 3 == 0 or i < 7 and j < 7:
                page.draw_rect(fitz.Rect(rect.x0 + i * s, rect.y0 + j * s, rect.x0 + (i + 1) * s,
                                         rect.y0 + (j + 1) * s), color=None, fill=(0, 0, 0))


def gia(number="2141438167", colour=False, number_elsewhere=False, tiny_right=False, qr_shift=0,
        metadata_number=True):
    doc = fitz.open()
    p = doc.new_page(width=792, height=612)
    p.insert_text((40, 30), "GIA  GEMOLOGICAL INSTITUTE OF AMERICA", fontsize=10)
    p.insert_text((250, 57), "GIA Report Number", fontsize=8)
    y1, y2, yi = (57, 125, 346) if not colour else (55, 144, 453)
    if tiny_right:     # inside the zone but past the masked 60%: must FAIL verification
        p.insert_text((410, y1), number, fontsize=4)
    else:
        p.insert_text((368, y1), number, fontsize=9)
    p.insert_text((150, y2), "Report", fontsize=7)
    p.insert_text((193, y2), number, fontsize=8)
    p.insert_text((40, yi), "Inscription:", fontsize=7)
    p.insert_text((96, yi), number, fontsize=7.5)
    if number_elsewhere:
        p.insert_text((40, 590), f"Verify report {number} online", fontsize=7)
    if colour:
        p.insert_text((40, 200), "Colored Diamond Grading Report", fontsize=9)
        p.insert_text((40, 212), "Color Origin ........ Natural", fontsize=8)
        p.insert_text((40, 224), "Color Grade ........ Fancy Intense Yellow", fontsize=8)
    else:
        p.insert_text((40, 200), "Shape and Cutting Style ........ Round Brilliant", fontsize=8)
        p.insert_text((40, 212), "Carat Weight ........ 1.01 carat", fontsize=8)
        p.insert_text((40, 224), "Color Grade ........ G", fontsize=8)
    qr = fitz.Rect(699 + qr_shift, 505, 751 + qr_shift, 557)
    _qr(p, qr)
    if metadata_number:
        doc.set_metadata({"title": f"GIA Report {number}", "author": "GIA", "subject": number})
    doc.embfile_add("data.txt", f"report {number}".encode())
    return doc.tobytes()


def igi(number="833689789"):
    doc = fitz.open()
    p = doc.new_page(width=1008, height=612)
    p.insert_text((40, 30), "IGI  INTERNATIONAL GEMOLOGICAL INSTITUTE", fontsize=10)
    p.insert_text((40, 60), "LABORATORY GROWN DIAMOND REPORT", fontsize=9)
    p.insert_text((362, 42), f"LG{number}", fontsize=8)
    p.insert_text((120, 157), "REPORT NUMBER", fontsize=7)
    p.insert_text((183, 157), f"LG{number}", fontsize=7)
    p.insert_text((120, 378), "INSCRIPTION(S):", fontsize=6)
    p.insert_text((185, 378), f"LG{number}", fontsize=7)
    p.insert_text((941, 80.5), f"LG{number}", fontsize=5.5)
    p.insert_text((934, 361), f"LG{number}", fontsize=6)
    p.insert_text((40, 200), "Shape and Cutting Style", fontsize=8)
    p.insert_text((40, 210), "ROUND BRILLIANT", fontsize=8)
    _qr(p, fitz.Rect(726, 502, 767, 543))
    doc.set_metadata({"title": f"IGI LG{number}"})
    return doc.tobytes()


def jpg():
    b = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 200, 200)).save(b, "JPEG")
    return b.getvalue()


def logo():
    return Image.new("RGBA", (120, 120), (20, 60, 120, 255))
