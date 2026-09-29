"""The 360 spinner (spin360.js) in a real headless browser: load order, full-quality swap-in, sharpness at
devicePixelRatio 2, auto-rotate pausing/resuming, and the pause button. Frames are generated here and
served in place of the media address (with a little latency); nothing goes to the network."""
import asyncio
import glob
import io
import os
import re
import time

import pytest
from PIL import Image, ImageDraw

pw = pytest.importorskip("playwright.async_api")
CHROME = (glob.glob("/opt/pw-browsers/chromium*/chrome-linux*/chrome") or [None])[0]
pytestmark = pytest.mark.skipif(not CHROME, reason="no headless Chromium available")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ORIGIN = "https://quote.alldiamondeverything.com"
N, TOP = 72, 10
ID5, ID6 = "a" * 32, "b" * 32


def frame(i, w, h, q):
    """A frame with a fine line grid (4 px period at 1600 wide) so sharpness shows, and its number."""
    big = Image.new("RGB", (1600, 1200), (30, 30, 30))
    d = ImageDraw.Draw(big)
    for x in range(0, 1600, 4):
        d.line([(x, 300), (x, 900)], fill=(235, 235, 235), width=2)
    d.text((40, 40), str(i), fill=(255, 255, 0))
    im = big if (w, h) == (1600, 1200) else big.resize((w, h), Image.LANCZOS)
    b = io.BytesIO()
    im.save(b, "WEBP", quality=q)
    return b.getvalue()


SMALL = {i: frame(i, 720, 540, 80) for i in range(N)}
HI = {i: frame(i, 1600, 1200, 90) for i in range(N)}
PAGE = f"""<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;background:#000">
<div id="box" style="width:400px;height:300px"></div>
<script src="/spin360.js"></script>
<script>window.t0 = performance.now();
window.sp = Spin360.mount(document.getElementById('box'), {{ id: location.hash.slice(1), n: {N}, top: {TOP}, v: Number(location.search.slice(1)) }});</script>
</body></html>"""


async def open_page(browser, v, dpr=2, log=None, delay=0.02):
    ctx = await browser.new_context(viewport={"width": 500, "height": 400}, device_scale_factor=dpr)
    page = await ctx.new_page()
    the_id = ID6 if v >= 6 else ID5

    async def handler(route):
        url = route.request.url
        path = url[len(ORIGIN):].split("?")[0]
        if path == "/spin360.js":
            return await route.fulfill(body=open(os.path.join(ROOT, "spin360.js"), "rb").read(),
                                       content_type="application/javascript")
        if path == "/test.html":
            return await route.fulfill(body=PAGE, content_type="text/html")
        m = re.fullmatch(r"/media/([a-f0-9]{32})/(\d{3})(@hi)?\.webp", path)
        if m:
            if log is not None:
                log.append((time.time(), int(m.group(2)), bool(m.group(3))))
            await asyncio.sleep(delay * (3 if m.group(3) else 1))
            i = int(m.group(2))
            return await route.fulfill(body=(HI if m.group(3) else SMALL)[i], content_type="image/webp")
        return await route.abort()
    await page.route(ORIGIN + "/**", handler)
    await page.goto(f"{ORIGIN}/test.html?{v}#{the_id}")
    return ctx, page


async def state(page):
    return await page.evaluate("""() => { const s = document.getElementById('box').__s360;
      return { pos: s.pos, loaded: s.loaded, hi: s.hiLoaded, drawnHi: s.drawnHi, coarseLeft: s.coarseLeft,
               paused: s.userPaused, w: s.canvas.width, h: s.canvas.height, dragging: !!s.dragging }; }""")


async def until(page, cond_js, timeout=15):
    t = time.time()
    while time.time() - t < timeout:
        if await page.evaluate(cond_js):
            return time.time() - t
        await asyncio.sleep(0.05)
    raise AssertionError("timed out waiting for " + cond_js)


async def sharpness(page):
    return await page.evaluate("""() => { const c = document.querySelector('#box canvas'), x = c.getContext('2d');
      const w = c.width, h = c.height, d = x.getImageData(Math.round(w*.3), Math.round(h*.4), Math.round(w*.4), Math.round(h*.2)).data;
      let s = 0, n = 0; for (let i = 0; i < d.length - 4; i += 4) { s += Math.abs(d[i] - d[i + 4]); n++; } return s / n; }""")


async def scenario_load_order_and_first_drag(browser):
    log6, log5 = [], []
    ctx6, p6 = await open_page(browser, 6, log=log6)
    t6 = await until(p6, "document.getElementById('box').__s360.coarseLeft === 0")
    await until(p6, "document.getElementById('box').__s360.hiLoaded === %d" % N)
    ctx5, p5 = await open_page(browser, 5, log=log5)
    t5 = await until(p5, "document.getElementById('box').__s360.coarseLeft === 0")
    await asyncio.sleep(1.0)
    # small set: the top frame first, then every 8th from it, then the rest — exactly as before
    small = [i for _, i, hi in log6 if not hi]
    assert small[0] == TOP
    assert {(i - TOP) % N for i in small[:9]} == {0, 8, 16, 24, 32, 40, 48, 56, 64}
    assert sorted(small) == list(range(N)) and len(small) == N
    # ... and every full-quality file is requested after the whole small set is in
    first_hi = min(t for t, i, hi in log6 if hi)
    last_small = max(t for t, i, hi in log6 if not hi)
    assert first_hi >= last_small - 0.05
    hi = [i for _, i, h in log6 if h]
    assert sorted(hi) == list(range(N))
    assert min((hi[0] - TOP) % N, (TOP - hi[0]) % N) <= 1                       # nearest-to-the-current frame first
    # a v5 capture (small set only) never asks for a full-quality file, and loads as before
    assert not any(h for _, _, h in log5) and sorted(i for _, i, _ in log5) == list(range(N))
    # time to first drag (coarse set in) is not slowed by the extra set
    assert t6 <= t5 * 1.5 + 0.3, (t6, t5)
    await ctx6.close(); await ctx5.close()
    return t6, t5


async def scenario_hi_swap_and_sharp(browser):
    ctx6, p6 = await open_page(browser, 6, dpr=2)
    await until(p6, "document.getElementById('box').__s360.coarseLeft === 0")
    early = await sharpness(p6)                                                 # small frames only so far (or few hi)
    st = await state(p6)
    assert st["w"] == 800 and st["h"] == 600                                    # canvas sized for devicePixelRatio 2
    await until(p6, "document.getElementById('box').__s360.hiLoaded === %d" % N)
    await asyncio.sleep(0.3)
    st = await state(p6)
    assert st["drawnHi"] is True                                                # the frame on screen is the full-quality one
    sharp6 = await sharpness(p6)
    ctx5, p5 = await open_page(browser, 5, dpr=2)
    await until(p5, "document.getElementById('box').__s360.loaded === %d" % N)
    await asyncio.sleep(0.3)
    sharp5 = await sharpness(p5)
    assert sharp6 > sharp5 * 1.15, (sharp6, sharp5)                             # visibly crisper at 2x
    # every frame swaps in while rotating: draw a few far-away frames and check they are hi too
    for f in (0, 20, 45, 71):
        await p6.evaluate("(f) => { const s = document.getElementById('box').__s360; s.pos = f; s.draw(true); }", f)
        assert (await state(p6))["drawnHi"] is True
    # 1x screen: canvas is 1x
    ctx1, p1 = await open_page(browser, 6, dpr=1)
    await until(p1, "document.getElementById('box').__s360.loaded === %d" % N)
    st1 = await state(p1)
    assert st1["w"] == 400 and st1["h"] == 300
    # 3x phone: sized for it
    ctx3, p3 = await open_page(browser, 6, dpr=3)
    await until(p3, "document.getElementById('box').__s360.loaded > 0")
    assert (await state(p3))["w"] == 1200
    for c in (ctx6, ctx5, ctx1, ctx3):
        await c.close()
    return early, sharp6, sharp5


async def moving(page, secs):
    a = (await state(page))["pos"]
    await asyncio.sleep(secs)
    return (await state(page))["pos"] - a


async def scenario_auto_rotate(browser):
    ctx, page = await open_page(browser, 6, dpr=1, delay=0.005)
    await until(page, "document.getElementById('box').__s360.loaded === %d" % N)
    # rotating on its own once everything is in
    await asyncio.sleep(1.6)                                                    # past the rest on the top frame
    assert await moving(page, 1.0) > 3
    # drag: it pauses while the user is dragging
    box = await page.locator("#box").bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    await page.mouse.move(cx, cy)
    await page.mouse.down()
    for k in range(1, 9):
        await page.mouse.move(cx + k * 12, cy)
        await asyncio.sleep(0.05)
    p_drag = (await state(page))["pos"]
    await asyncio.sleep(1.0)
    assert abs((await state(page))["pos"] - p_drag) < 0.01                     # held still: no auto-rotate under the finger
    await page.mouse.up()
    t_up = time.time()
    p_up = (await state(page))["pos"]
    # 3 s of no interaction: still, then it resumes
    await asyncio.sleep(2.0)
    p2 = (await state(page))["pos"]
    assert abs(p2 - p_up) < 4                                                   # (only a flick's inertia, no auto-rotate)
    await asyncio.sleep(max(0, 3.0 - (time.time() - t_up)) + 0.25)
    s1 = (await state(page))["pos"]
    await asyncio.sleep(0.3)
    s2 = (await state(page))["pos"]
    await asyncio.sleep(1.2)
    s3 = (await state(page))["pos"]
    assert abs(s1 - p2) < 4                                                     # resumes from the current frame, no jump
    assert s3 - s2 > 2                                                          # and is rotating again
    assert (s2 - s1) < (s3 - s2)                                                # eased in: slower at first, then up to speed
    # a touch during the wait restarts the 3 s
    await page.mouse.move(cx, cy); await page.mouse.down(); await page.mouse.move(cx + 20, cy); await page.mouse.up()
    await asyncio.sleep(2.0)
    a = (await state(page))["pos"]
    await asyncio.sleep(0.5)
    assert abs((await state(page))["pos"] - a) < 1.5
    await ctx.close()


async def scenario_pause_button(browser):
    ctx, page = await open_page(browser, 6, dpr=1, delay=0.005)
    await until(page, "document.getElementById('box').__s360.loaded === %d" % N)
    await asyncio.sleep(1.6)
    btn = page.locator("#box .s360-btn")
    assert await btn.count() == 1 and await btn.get_attribute("aria-label") == "Pause automatic rotation"
    assert await moving(page, 0.8) > 2
    await btn.click()
    assert await btn.get_attribute("data-state") == "paused" and await btn.get_attribute("aria-label") == "Play automatic rotation"
    p = (await state(page))["pos"]
    await asyncio.sleep(4.0)                                                    # well past the 3 s: no auto-resume
    assert abs((await state(page))["pos"] - p) < 0.01 and (await state(page))["paused"] is True
    # dragging while paused doesn't bring it back either
    box = await page.locator("#box").bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    await page.mouse.move(cx, cy); await page.mouse.down(); await page.mouse.move(cx + 60, cy); await page.mouse.up()
    await asyncio.sleep(3.0)                                                    # the flick's inertia has died away
    p = (await state(page))["pos"]
    await asyncio.sleep(4.0)
    assert abs((await state(page))["pos"] - p) < 0.01
    # the button doesn't start a drag
    assert (await state(page))["dragging"] is False
    # play: rotating again (from where it is)
    await btn.click()
    assert await btn.get_attribute("data-state") == "playing"
    await asyncio.sleep(1.2)
    assert await moving(page, 1.0) > 2
    await ctx.close()


def run(scenario):
    async def go():
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME)
            try:
                return await scenario(browser)
            finally:
                await browser.close()
    return asyncio.run(go())


def test_load_order_unchanged_and_first_drag_not_slower():
    run(scenario_load_order_and_first_drag)


def test_full_quality_frames_swap_in_and_are_sharp_at_dpr_2():
    early, sharp6, sharp5 = run(scenario_hi_swap_and_sharp)
    assert sharp6 > sharp5


def test_auto_rotate_pauses_on_touch_and_resumes_after_3_seconds():
    run(scenario_auto_rotate)


def test_pause_button_stops_auto_resume_until_play():
    run(scenario_pause_button)
