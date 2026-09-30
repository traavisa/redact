"""Idar Jewellers: built in like Pure Diamond and Spence (both pickers, quote/share pages, link preview
cards, certificate QR replacement, favicons). Fake data only."""
import base64
import io
import json
import os
import re
import subprocess

from PIL import Image

import cert_attach as C
import fakecerts as F
from test_app import APP, boot, env, texts  # noqa: F401  (env is a fixture)
from test_clients_icons import _app_b64, _icon, _node

HERE = os.path.dirname(APP)
ROOT = os.path.dirname(HERE)


def test_idar_is_built_in_and_the_logo_file_was_moved():
    a = open(os.path.join(HERE, "app.py")).read()
    q = open(os.path.join(ROOT, "quote.html")).read()
    assert '"Idar Jewellers": CLIENT_Idar_Jewellers_B64,' in a and a.count('"Idar Jewellers"') == 2
    m = re.search(r'^  "Idar Jewellers": "data:image/jpeg;base64,([A-Za-z0-9+/=]+)"$', q, re.M)
    assert m and m.group(1) == _app_b64("Idar_Jewellers")
    assert base64.b64decode(m.group(1)) == open(os.path.join(ROOT, "logos", "Idar.jpg"), "rb").read()
    assert not os.path.exists(os.path.join(ROOT, "new_logos"))


def test_idar_pickers_and_certificate_qr(env):
    sb, api = env
    at = boot()
    at.button(key="ovl_t2_client_Idar Jewellers").click().run()
    assert at.session_state["q_client_sel"] == "Idar Jewellers" and not at.exception
    assert "Quoting for **Idar Jewellers**" in texts(at)
    at.radio(key="ls_mode").set_value("Look up stones").run()
    at.text_area(key="ls_lk_text").input("LG833689789").run()
    at.button(key="ls_lk_go").click().run()
    at.button(key="ovl_ls_client_Idar Jewellers").click().run()
    assert at.session_state["ls_client_sel"] == "Idar Jewellers"
    at.button(key="ls_add_quote").click().run()
    assert not at.exception, at.exception
    assert sb.tables["quotes"][-1]["client"] == "Idar Jewellers"
    pdf = next(iter(sb.storage["certificates"].values()))
    logo = Image.open(io.BytesIO(base64.b64decode(_app_b64("Idar_Jewellers")))).convert("RGBA")
    assert C.verify(pdf, {"833689789"}, "IGI", logo) == []
    assert "QR area" in C.verify(pdf, {"833689789"}, "IGI", F.logo())


JS = r"""
const Module = require('module'), orig = Module._load;
const Q = { id: 'idq1', client: 'Idar Jewellers', expires_at: new Date(Date.now() + 864e5).toISOString(),
  stones: [{ cert_data: { shape: 'round', carat: 1.01 }, price: '999' }] };
Module._load = function (r, ...a) {
  if (r === '@supabase/supabase-js') return { createClient: () => ({ from: () => ({ select: () => ({ eq: (k, id) => ({
    single: async () => ({ data: id === 'idq1' ? Q : null }) }) }) }) }) };
  return orig.call(this, r, ...a);
};
global.fetch = async (u) => { u = String(u);
  if (/quotes\?id=eq\.idq1/.test(u)) return { ok: true, json: async () => [Q] };
  return { ok: true, json: async () => [] }; };
process.env.SUPABASE_URL = 'https://db.test'; process.env.SUPABASE_SERVICE_KEY = 'k';
const H = { host: 'quote.alldiamondeverything.com', 'user-agent': 'Slackbot-LinkExpanding 1.0' };
const f = (n) => require('./netlify/functions/' + n);
(async () => {
  const o = {};
  const qp = await f('quote-page.js').handler({ path: '/q/idq1', headers: H });
  const cp = await f('customer-page.js').handler({ path: '/c/idar-jewellers/abc123', headers: H });
  o.q = qp.body; o.c = cp.body;
  for (const [k, p] of Object.entries({ oq: '/og/q/idq1.jpg', oc: '/og/c/idar-jewellers.jpg', lg: null })) {
    if (!p) continue;
    const r = await f('og-image.js').handler({ path: p, headers: H });
    o[k] = { s: r.statusCode, t: r.headers['Content-Type'], b: r.body };
  }
  const lg = await f('client-logo.js').handler({ path: '/client-logo/idar-jewellers' });
  o.lg = { s: lg.statusCode, t: lg.headers['Content-Type'] };
  o.icons = {};
  for (const n of [32, 180, 512]) {
    const r = await f('client-logo.js').handler({ path: `/client-logo/idar-jewellers/icon-${n}.png` });
    o.icons[n] = { s: r.statusCode, src: r.headers['X-Icon-Source'], b: r.body };
  }
  process.stdout.write(JSON.stringify(o));
})();
"""


def test_idar_pages_previews_and_favicons():
    d = _node(JS)
    for html in (d["q"], d["c"]):
        tags = re.findall(r'<link rel="(?:icon|apple-touch-icon)"[^>]*>', html)
        assert len(tags) == 3 and all("/client-logo/idar-jewellers/icon-" in t for t in tags)
    assert "const STORE_NAME = \"Idar Jewellers\"" in d["c"] and "Idar Jewellers — Diamond Selection" in d["c"]
    assert "quote.alldiamondeverything.com/og/q/idq1.jpg" in d["q"]
    assert "quote.alldiamondeverything.com/og/c/idar-jewellers.jpg" in d["c"]
    for k in ("oq", "oc"):
        assert d[k]["s"] == 200 and d[k]["t"] == "image/jpeg", k
        im = Image.open(io.BytesIO(base64.b64decode(d[k]["b"])))
        assert im.size == (1200, 630) and len(d[k]["b"]) * 3 // 4 < 300_000, k
        assert sum(1 for r, g, b in im.convert("RGB").crop((520, 90, 680, 240)).getdata() if r > 220 and 170 < g < 220 and b < 90) > 3000, k  # the yellow bee is on the card
    assert d["lg"] == {"s": 200, "t": "image/jpeg"}
    for n in ("32", "180", "512"):
        e = d["icons"][n]
        assert e["src"] == "client"
        assert _icon({"status": 200, "type": "image/png", "b64": e["b"]}).size == (int(n),) * 2
