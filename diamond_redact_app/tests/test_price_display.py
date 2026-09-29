"""Quote and share pages: the price is drawn with lining numerals and nothing is clipped (headless
browser, desktop and phone widths). Needs Chromium and the web font (skipped when either is missing)."""
import asyncio
import base64
import glob
import io
import os
import re

import pytest
from PIL import Image

pw = pytest.importorskip("playwright.async_api")
CHROME = (glob.glob("/opt/pw-browsers/chromium*/chrome-linux*/chrome") or [None])[0]
pytestmark = pytest.mark.skipif(not CHROME, reason="no headless Chromium available")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FONT_URL = ("https://fonts.gstatic.com/s/cormorantgaramond/v21/"
            "co3umX5slCNuHLi8bLeY9MK7whWMhyjypVO7abI26QOD_v86GnM.ttf")


def _font():
    import requests
    try:
        r = requests.get(FONT_URL, timeout=15)
        return r.content if r.status_code == 200 else None
    except Exception:
        return None


def _ink_rows(png):
    im = Image.open(io.BytesIO(png)).convert("L")
    w, h = im.size
    px = im.load()
    return sum(1 for y in range(h) if any(px[x, y] > 90 for x in range(w)))


@pytest.mark.parametrize("page", ["quote.html", "customer.html"])
def test_price_is_not_clipped_and_uses_lining_numerals(page):
    font = _font()
    if not font:
        pytest.skip("web font not reachable")
    src = open(os.path.join(ROOT, page)).read()
    css = re.search(r"    \.price-amount \{.*?\n    \}\n", src, re.S).group(0)
    assert "price-currency-badge" not in src                                   # the redundant CAD badge is gone
    face = ('<style>@font-face{font-family:"Cormorant Garamond";font-weight:400;src:url(data:font/ttf;base64,'
            + base64.b64encode(font).decode() + ')}</style>')

    def html(solid):
        extra = ".price-amount{background:none!important;-webkit-text-fill-color:#fff!important}" if solid else ""
        return (f'<!doctype html><meta charset=utf-8>{face}<style>body{{background:#0f0f0f;padding:30px}}{css}{extra}</style>'
                '<div class="price-block"><div class="price-amount" id=p>CA$897 CA$1,234,567</div></div>')

    async def run():
        async with pw.async_playwright() as p:
            b = await p.chromium.launch(executable_path=CHROME)
            out = []
            for width in (1280, 390):
                pg = await b.new_page(viewport={"width": width, "height": 300}, device_scale_factor=2)
                rows = []
                for solid in (True, False):
                    await pg.set_content(html(solid))
                    await pg.evaluate("document.fonts.ready")
                    await pg.wait_for_timeout(500)
                    rows.append(_ink_rows(await pg.screenshot()))
                nums = await pg.evaluate("getComputedStyle(document.getElementById('p')).fontVariantNumeric")
                out.append((width, rows, nums))
            await b.close()
            return out
    for width, (plain, clipped), nums in asyncio.run(run()):
        assert plain == clipped, f"{page} at {width}px: {plain - clipped} pixel rows of the price are cut off"
        assert "lining-nums" in nums
