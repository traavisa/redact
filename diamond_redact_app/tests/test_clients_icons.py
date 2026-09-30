"""Pure Diamond, Bell Diamonds' own logo, per-client favicons on every client-facing page, and a
failed custom_clients read. All with fake data (fake Supabase / fake database responses)."""
import base64
import io
import json
import logging
import os
import re
import subprocess

import pytest
import requests
from PIL import Image

import cert_attach as C
import fakecerts as F
from test_app import APP, boot, env, texts  # noqa: F401  (env is a fixture)

HERE = os.path.dirname(APP)
ROOT = os.path.dirname(HERE)


def _app_b64(const):
    m = re.search(r'^CLIENT_%s_B64 = "([A-Za-z0-9+/=]+)"' % const, open(os.path.join(HERE, "app.py")).read(), re.M)
    return m.group(1)


def _png(size, color, mode="RGB", fmt="PNG"):
    b = io.BytesIO()
    Image.new(mode, size, color).save(b, fmt)
    return base64.b64encode(b.getvalue()).decode()


def _node(js):
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ── Pure Diamond: built in like Spence ───────────────────────────────────────────────
def test_pure_diamond_is_built_in_everywhere():
    a = open(os.path.join(HERE, "app.py")).read()
    q = open(os.path.join(ROOT, "quote.html")).read()
    assert '"Pure Diamond": CLIENT_Pure_Diamond_B64,' in a and a.count('"Pure Diamond"') == 2   # LOGOS + ORDER
    m = re.search(r'^  "Pure Diamond": "data:image/jpeg;base64,([A-Za-z0-9+/=]+)",$', q, re.M)
    assert m and m.group(1) == _app_b64("Pure_Diamond")                                        # app == quote page
    got = Image.open(io.BytesIO(base64.b64decode(m.group(1)))).convert("RGB")
    ref = Image.open(os.path.join(ROOT, "logos", "PureDiamond.jpg")).convert("RGB").resize(got.size)
    diff = sum(abs(x - y) for p1, p2 in zip(got.resize((16, 16)).getdata(), ref.resize((16, 16)).getdata())
               for x, y in zip(p1, p2)) / (16 * 16 * 3)
    assert diff < 4                                                                            # it is the uploaded logo
    assert not os.path.exists(os.path.join(ROOT, "new_logos"))


def test_pure_diamond_in_both_pickers_and_certificate_qr(env):
    sb, api = env
    at = boot()
    assert any(b.key == "ovl_t2_client_Pure Diamond" for b in at.button)                       # Create quote picker
    at.button(key="ovl_t2_client_Pure Diamond").click().run()
    assert at.session_state["q_client_sel"] == "Pure Diamond" and not at.exception
    at.radio(key="ls_mode").set_value("Look up stones").run()
    at.text_area(key="ls_lk_text").input("LG833689789").run()
    at.button(key="ls_lk_go").click().run()
    assert any(b.key == "ovl_ls_client_Pure Diamond" for b in at.button)                       # Live Search picker
    at.button(key="ovl_ls_client_Pure Diamond").click().run()
    assert at.session_state["ls_client_sel"] == "Pure Diamond"
    at.button(key="ls_add_quote").click().run()
    assert not at.exception, at.exception
    assert sb.tables["quotes"][-1]["client"] == "Pure Diamond"
    pdf = next(iter(sb.storage["certificates"].values()))
    logo = Image.open(io.BytesIO(base64.b64decode(_app_b64("Pure_Diamond")))).convert("RGBA")
    assert C.verify(pdf, {"833689789"}, "IGI", logo) == []                                      # QR area = Pure Diamond's logo
    assert "QR area" in C.verify(pdf, {"833689789"}, "IGI", F.logo())


# ── Bell Diamonds shows its own logo ─────────────────────────────────────────────────
def test_bell_diamonds_logo_is_its_own_and_cannot_be_overridden(env):
    import streamlit as st
    sb, api = env
    q = open(os.path.join(ROOT, "quote.html")).read()
    bell = re.search(r'^  "Bell Diamonds": "data:image/png;base64,([A-Za-z0-9+/=]+)",$', q, re.M).group(1)
    pcg = _app_b64("Pure_Carbon_Group")
    assert bell == _app_b64("Bell_Diamonds") and bell != pcg
    got = Image.open(io.BytesIO(base64.b64decode(bell))).convert("RGB")
    ref = Image.open(os.path.join(ROOT, "logos", "Bell Diamonds.png")).convert("RGB").resize(got.size)
    small = lambda im: list(im.resize((16, 16)).getdata())  # noqa: E731
    assert sum(abs(x - y) for a, b in zip(small(got), small(ref)) for x, y in zip(a, b)) / (16 * 16 * 3) < 4
    # A bad record saved in the app under the same name (Pure Carbon's logo) must not replace it
    st.cache_data.clear()
    sb.tables["custom_clients"].append({"name": "Bell Diamonds", "logo_b64": pcg})
    at = boot()
    cards = [m.value for m in at.markdown if "Bell Diamonds" in m.value and "<img" in m.value]
    assert cards and bell[:200] in cards[0] and pcg[:200] not in cards[0]
    at.radio(key="ls_mode").set_value("Look up stones").run()
    at.session_state["ls_client_sel"] = "Bell Diamonds"
    at.text_area(key="ls_lk_text").input("LG833689789").run()
    at.button(key="ls_lk_go").click().run()
    at.button(key="ls_add_quote").click().run()
    assert not at.exception, at.exception
    pdf = next(iter(sb.storage["certificates"].values()))
    assert C.verify(pdf, {"833689789"}, "IGI", Image.open(io.BytesIO(base64.b64decode(bell))).convert("RGBA")) == []
    # ...and on the pages
    d = _node("""
      global.fetch = async () => ({ ok: true, json: async () => [{ name: 'Bell Diamonds', logo_b64: process.argv[1] }] });
      process.env.SUPABASE_URL = 'https://db.test'; process.env.SUPABASE_SERVICE_KEY = 'k';
      const logo = require('./netlify/functions/client-logo.js');
      const page = require('./netlify/functions/customer-page.js');
      (async () => {
        const r = await logo.handler({ path: '/client-logo/bell-diamonds' });
        const c = await page.handler({ path: '/c/bell-diamonds/abc', headers: { host: 'x.test' } });
        process.stdout.write(JSON.stringify({ logo: r.body, page: c.body.includes(process.argv[2]) }));
      })();
    """.replace("process.argv[1]", json.dumps(pcg)).replace("process.argv[2]", json.dumps(bell[:200])))
    assert d["logo"] == bell and d["page"]


# ── A failed custom_clients read must not crash the Create quote tab ──────────────────
WARN = "Couldn't load added clients, showing built-in clients only"


def test_failed_custom_clients_read_shows_built_in_clients_and_a_warning(env, caplog):
    import streamlit as st
    sb, api = env
    sb.tables["custom_clients"].append({"name": "Zed & Co", "logo_b64": _png((40, 40), (200, 30, 30))})
    st.cache_data.clear()
    good = boot()
    assert any(b.key == "ovl_t2_client_Zed & Co" for b in good.button)
    assert WARN not in texts(good)                                            # no warning when it works
    for mode in ("http", "network"):
        st.cache_data.clear()
        if mode == "http":
            sb.block_reads = True                                                              # database answers 401
        else:
            sb.block_reads = False
            real = requests.get

            def boom(url, *a, **k):
                if "custom_clients" in str(url):
                    raise requests.ConnectionError("database down")
                return real(url, *a, **k)
            requests.get = boom
        try:
            caplog.clear()
            with caplog.at_level(logging.ERROR):
                at = boot()
            assert not at.exception, (mode, at.exception)
            assert WARN in texts(at), mode
            keys = {b.key for b in at.button}
            assert {"ovl_t2_client_Pure Carbon Group", "ovl_t2_client_Spence", "ovl_t2_client_Pure Diamond"} <= keys
            assert "ovl_t2_client_Zed & Co" not in keys                                       # built-in clients only
            assert any("custom_clients read failed" in r.getMessage() for r in caplog.records), mode
            at.button(key="ovl_t2_client_Bell Diamonds").click().run()                         # and the tab still works
            assert not at.exception and at.session_state["q_client_sel"] == "Bell Diamonds"
        finally:
            if mode == "network":
                requests.get = real
    sb.block_reads = False
    st.cache_data.clear()
    assert any(b.key == "ovl_t2_client_Zed & Co" for b in boot().button)                      # recovers on its own (nothing cached)


# ── Favicons ─────────────────────────────────────────────────────────────────────────
ICON_JS = """
const Module = require('module'), orig = Module._load;
const QUOTES = { qbell: { client: 'Bell Diamonds', stones: [] }, qpd: { client: 'Pure Diamond', stones: [] },
                 qzed: { client: 'Zed & Co', stones: [] }, qold: { client: 'Gone Client', stones: [] } };
Module._load = function (r, ...a) {
  if (r === '@supabase/supabase-js') return { createClient: () => ({ from: () => ({ select: () => ({ eq: (k, id) => ({
    single: async () => ({ data: QUOTES[id] || null }) }) }) }) }) };
  return orig.call(this, r, ...a);
};
const rows = %s;
global.fetch = async () => ({ ok: true, json: async () => rows });
process.env.SUPABASE_URL = 'https://db.test'; process.env.SUPABASE_SERVICE_KEY = 'k';
const logo = require('./netlify/functions/client-logo.js');
const H = { host: 'quote.alldiamondeverything.com' };
const hrefs = (html) => [...html.matchAll(/<link rel="(?:icon|apple-touch-icon)"[^>]*>/g)].map((m) => m[0]);
(async () => {
  const out = { pages: {}, icons: {} };
  const pages = { q: require('./netlify/functions/quote-page.js'), c: require('./netlify/functions/customer-page.js'),
                  s: require('./netlify/functions/share-page.js') };
  const paths = { 'q/qbell': ['q', '/q/qbell'], 'q/qpd': ['q', '/q/qpd'], 'q/qzed': ['q', '/q/qzed'], 'q/none': ['q', '/q/nothing'],
    'c/pd': ['c', '/c/pure-diamond/abc'], 'c/bell': ['c', '/c/bell-diamonds/abc'], 'c/zed': ['c', '/c/zed-and-co/abc'],
    'c/nobody': ['c', '/c/nobody/abc'], 's/old': ['s', '/s/abc'] };
  for (const [k, [p, path]] of Object.entries(paths)) {
    const r = await pages[p].handler({ path, headers: H });
    out.pages[k] = { tags: hrefs(r.body), all: (r.body.match(/<link rel="[^"]*icon"[^>]*>/g) || []).length };
  }
  const index = require('fs').readFileSync('index.html', 'utf8');
  out.viewer = hrefs(index);
  const urls = new Set();
  Object.values(out.pages).forEach((p) => p.tags.forEach((t) => urls.add(/href="([^"]+)"/.exec(t)[1])));
  out.viewer.forEach((t) => urls.add(/href="([^"]+)"/.exec(t)[1]));
  for (const slug of %s) for (const n of [32, 180, 512]) urls.add(`/client-logo/${slug}/icon-${n}.png`);
  for (const u of urls) {
    const r = await logo.handler({ path: u });
    out.icons[u] = { status: r.statusCode, type: (r.headers || {})['Content-Type'], src: (r.headers || {})['X-Icon-Source'], b64: r.body };
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def _run_icons(rows, extra_slugs=()):
    builtin = ["pure-carbon-group", "nash-jewellers", "nfr", "cavalier", "foe-and-dear", "harlings", "rodan", "janinas",
               "ijl", "gem-by-carati", "vena-nova", "touch-of-gold", "bijouterie-italienne", "perrara", "bell-diamonds",
               "barclays", "spence", "pure-diamond", "idar-jewellers"]
    return _node(ICON_JS % (json.dumps(rows), json.dumps(builtin + list(extra_slugs))))


def _icon(entry):
    assert entry["status"] == 200 and entry["type"] == "image/png", entry["status"]
    return Image.open(io.BytesIO(base64.b64decode(entry["b64"]))).convert("RGB")


def _wide_logo():                                              # in-app client: 90x40, red, transparent top/bottom rows
    im = Image.new("RGBA", (90, 40), (200, 30, 30, 255))
    for x in range(90):
        im.putpixel((x, 0), (0, 0, 0, 0)); im.putpixel((x, 39), (0, 0, 0, 0))
    b = io.BytesIO(); im.save(b, "PNG")
    return base64.b64encode(b.getvalue()).decode()


ZED = _wide_logo()
BAD = base64.b64encode(b"RIFFxxxxWEBPVP8 ").decode()           # in-app client whose logo isn't PNG/JPEG
ROWS = [{"name": "Zed & Co", "logo_b64": ZED}, {"name": "Broken Co", "logo_b64": BAD}]


def _tag_slug(tag):
    return re.search(r"/client-logo/([^/]+)/icon-(\d+)\.png", tag).groups()


def test_every_page_type_uses_the_clients_icon_with_apple_touch_icon():
    d = _run_icons(ROWS, ["zed-and-co", "broken-co", "no-such-client"])
    expect = {"q/qbell": "bell-diamonds", "q/qpd": "pure-diamond", "q/qzed": "zed-and-co", "q/none": "pure-carbon-group",
              "c/pd": "pure-diamond", "c/bell": "bell-diamonds", "c/zed": "zed-and-co", "c/nobody": "pure-carbon-group",
              "s/old": "pure-carbon-group"}
    for page, slug in expect.items():
        tags = d["pages"][page]["tags"]
        assert d["pages"][page]["all"] == 3 == len(tags), page                                # exactly the three icon tags, no stale one
        assert {(_tag_slug(t)[0], _tag_slug(t)[1]) for t in tags} == {(slug, "32"), (slug, "512"), (slug, "180")}, page
        assert any('rel="apple-touch-icon"' in t and 'sizes="180x180"' in t for t in tags), page
        for t in tags:
            assert d["icons"][re.search(r'href="([^"]+)"', t).group(1)]["status"] == 200      # every linked icon resolves
    assert {_tag_slug(t)[0] for t in d["viewer"]} == {"pure-carbon-group"}                    # viewer default
    idx = open(os.path.join(ROOT, "index.html")).read()
    assert "params.get('c')" in idx and "[a-z0-9-]{1,80}" in idx                              # viewer takes ?c=<client>
    for f in ("quote.html", "customer.html"):
        html = open(os.path.join(ROOT, f)).read()
        assert 'href="data:' not in re.search(r'<link rel="icon"[^>]*>', html).group(0)
    assert "getElementById('favicon').href" not in open(os.path.join(ROOT, "customer.html")).read()


def test_icons_are_square_padded_not_stretched_and_fall_back_only_without_a_usable_logo():
    d = _run_icons(ROWS, ["zed-and-co", "broken-co", "no-such-client"])
    for slug in ("pure-diamond", "bell-diamonds", "barclays", "touch-of-gold", "spence", "zed-and-co"):
        for n in (32, 180, 512):
            im = _icon(d["icons"][f"/client-logo/{slug}/icon-{n}.png"])
            assert im.size == (n, n), (slug, n)
    # Barclay's (350x264) and Zed (90x40): the logo keeps its aspect and is centred on padding
    w = _icon(d["icons"]["/client-logo/zed-and-co/icon-512.png"])
    rows_with_logo = [y for y in range(512) if w.getpixel((256, y))[0] > 150 and w.getpixel((256, y))[1] < 80]
    span = max(rows_with_logo) - min(rows_with_logo) + 1
    assert abs(span - 512 * 0.88 * 38 / 90) < 6                                               # 90:40 ratio, not stretched
    assert w.getpixel((5, 5)) == (255, 255, 255)       # padding = a solid background
    # a client's own icon differs from the fallback; unknown / unusable logos get the Pure Carbon icon
    pcg = d["icons"]["/client-logo/pure-carbon-group/icon-180.png"]["b64"]
    assert d["icons"]["/client-logo/bell-diamonds/icon-180.png"]["b64"] != pcg
    assert d["icons"]["/client-logo/pure-diamond/icon-180.png"]["b64"] != pcg
    assert d["icons"]["/client-logo/zed-and-co/icon-180.png"]["src"] == "client"
    for slug in ("no-such-client", "broken-co"):
        e = d["icons"][f"/client-logo/{slug}/icon-180.png"]
        assert e["src"] == "fallback" and e["b64"] == pcg, slug
    assert d["icons"]["/client-logo/bell-diamonds/icon-180.png"]["src"] == "client"


def test_every_built_in_client_logo_makes_a_usable_icon():
    slugs = ["pure-carbon-group", "nash-jewellers", "nfr", "cavalier", "foe-and-dear", "harlings", "rodan", "janinas",
             "ijl", "gem-by-carati", "vena-nova", "touch-of-gold", "bijouterie-italienne", "perrara", "bell-diamonds",
             "barclays", "spence", "pure-diamond", "idar-jewellers"]
    d = _run_icons([])
    unusable = [s for s in slugs if d["icons"][f"/client-logo/{s}/icon-180.png"]["src"] != "client"]
    assert unusable == []


def test_client_logo_still_serves_the_plain_logo():
    d = _node("""
      global.fetch = async () => ({ ok: true, json: async () => [] });
      const logo = require('./netlify/functions/client-logo.js');
      (async () => { const r = await logo.handler({ path: '/client-logo/pure-diamond' });
        const bad = await logo.handler({ path: '/client-logo/pure-diamond/icon-99.png' });
        process.stdout.write(JSON.stringify({ t: r.headers['Content-Type'], n: r.body.length, bad: bad.statusCode })); })();
    """)
    assert d["t"] == "image/jpeg" and d["n"] > 1000 and d["bad"] == 404
