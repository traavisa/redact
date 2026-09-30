"""Link previews: server-side Open Graph / Twitter tags on /q/… and /c/… pages, and the 1200x630
preview images (fake data: fake database rows, fake re-hosted stone photos)."""
import base64
import io
import json
import os
import re
import subprocess

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(HERE)
MEDIA = "https://quote.alldiamondeverything.com/media/"
SLACK = "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)"

HARNESS = r"""
const fs = require('fs'), Module = require('module');
const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const QUOTES = cfg.quotes; QUOTES.forEach((q) => { q.expires_at = new Date(Date.now() + q.expires_in * 1000).toISOString(); });
const byId = Object.fromEntries(QUOTES.map((q) => [q.id, q]));
const orig = Module._load;
Module._load = function (r, ...a) {
  if (r === '@supabase/supabase-js') return { createClient: () => ({ from: () => ({ select: () => ({ eq: (k, id) => ({
    single: async () => ({ data: byId[id] || null }) }) }) }) }) };
  return orig.call(this, r, ...a);
};
global.fetch = async (url) => {
  url = String(url);
  if (url.includes('/rest/v1/custom_clients')) return { ok: true, json: async () => cfg.custom };
  let m = /quotes\?id=eq\.([a-z0-9]+)/.exec(url);
  if (m) return { ok: true, json: async () => (byId[m[1]] ? [byId[m[1]]] : []) };
  m = /quote-media\/(.+)$/.exec(url);
  if (m) { const f = cfg.media + '/' + m[1]; return fs.existsSync(f) ? { ok: true, arrayBuffer: async () => fs.readFileSync(f) } : { ok: false }; }
  throw new Error('unexpected fetch ' + url);
};
process.env.SUPABASE_URL = 'https://db.test'; process.env.SUPABASE_SERVICE_KEY = 'k';
const F = (n) => require(cfg.root + '/netlify/functions/' + n);
(async () => {
  const out = { pages: {}, images: {} };
  const H = { host: 'quote.alldiamondeverything.com', 'user-agent': cfg.ua };
  for (const [k, [fn, path]] of Object.entries(cfg.pages)) {
    const r = await F(fn).handler({ path, headers: H });
    out.pages[k] = { status: r.statusCode, body: r.body };
  }
  for (const [k, path] of Object.entries(cfg.images)) {
    const r = await F('og-image.js').handler({ path, headers: H });
    out.images[k] = { status: r.statusCode, headers: r.headers, b64: r.body };
  }
  out.svgs = {};
  const P = require(cfg.root + '/netlify/functions/lib/preview.js');
  for (const q of QUOTES) out.svgs[q.id] = P.summarize(q.stones).text;
  process.stdout.write(JSON.stringify(out));
})();
"""


def _photo(path, size, bg, fmt):
    im = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(im)
    cx, cy, r = size[0] // 2, size[1] // 2, min(size) // 3
    d.polygon([(cx - r, cy), (cx, cy - r), (cx + r, cy), (cx, cy + r)], fill=(80, 200, 255))
    im.save(path, fmt)


def _logo_b64(color):
    b = io.BytesIO()
    Image.new("RGB", (60, 60), color).save(b, "PNG")
    return base64.b64encode(b.getvalue()).decode()


def _stone(shape, carat, image=None, **extra):
    s = {"cert_data": {"shape": shape, "carat": carat}, "price": "4123", "price_type": "stone", "cert_last4": "9789",
         "pdf_url": "https://quote.alldiamondeverything.com/certs/" + "c" * 32 + ".pdf"}
    if image:
        s["image_url"] = MEDIA + image
    s.update(extra)
    return s


A, B = "a" * 32 + ".jpg", "b" * 32 + ".png"
QUOTES = [
    {"id": "cav1", "client": "Cavalier", "expires_in": 86400 * 10,
     "stones": [_stone("CUSHION", "1.52", A), _stone("Cushion", "2.10"), _stone("cushion", "1.80")]},
    {"id": "pd01", "client": "Pure Diamond", "expires_in": 86400 * 10,
     "stones": [_stone("Round", "1.00", None, video_url=MEDIA + B)]},                 # a still stored as the "video"
    {"id": "zed1", "client": "Zed & Co", "expires_in": 86400 * 10, "stones": [_stone("Oval", "1.20", B), _stone("Oval", "1.50")]},
    {"id": "bell", "client": "Bell Diamonds", "expires_in": 86400 * 10,
     "stones": [_stone("Oval", "1.02", A), _stone("Round", "3.01"), _stone("Pear", "2.2")]},
    {"id": "noim", "client": "Nash Jewellers", "expires_in": 86400 * 10, "stones": [_stone("Cushion", "1.52"), _stone("Cushion", "2.10")]},
    {"id": "vend", "client": "Spence", "expires_in": 86400 * 10,                       # vendor-ish media must never be used
     "stones": [_stone("Round", "1.00", None, image_url="https://cdn.vendor.example/pic.jpg")]},
    {"id": "gone", "client": "Cavalier", "expires_in": -3600, "stones": [_stone("Cushion", "1.52", A)]},
    {"id": "soon", "client": "Cavalier", "expires_in": 600, "stones": [_stone("Cushion", "1.52", A)]},
]


def _run(tmp_path, pages=None, images=None):
    _photo(tmp_path / A, (1600, 1200), (20, 20, 24), "JPEG")
    _photo(tmp_path / B, (900, 900), (235, 235, 235), "PNG")
    cfg = {"root": ROOT, "ua": SLACK, "media": str(tmp_path), "quotes": QUOTES,
           "custom": [{"name": "Zed & Co", "logo_b64": _logo_b64((200, 30, 30))}],
           "pages": pages or {}, "images": images or {}}
    (tmp_path / "cfg.json").write_text(json.dumps(cfg))
    (tmp_path / "h.js").write_text(HARNESS)
    out = subprocess.run(["node", str(tmp_path / "h.js"), str(tmp_path / "cfg.json")], capture_output=True, text=True,
                         timeout=180, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _img(entry):
    assert entry["status"] == 200 and entry["headers"]["Content-Type"] == "image/jpeg"
    raw = base64.b64decode(entry["b64"])
    im = Image.open(io.BytesIO(raw))
    assert im.format == "JPEG" and im.size == (1200, 630)
    assert len(raw) < 300 * 1024                                    # WhatsApp drops larger preview images
    return im.convert("RGB")


def _meta(html, key):
    m = re.search(r'<meta (?:property|name)="%s" content="([^"]*)"' % re.escape(key), html)
    return m.group(1) if m else None


def _stone_blue(im, x0=700):                                        # the test photo's cyan diamond, right-hand panel
    px = im.crop((x0, 0, 1200, 630)).resize((50, 63)).getdata()
    return sum(1 for r, g, b in px if b > 200 and g > 150 and r < 140)


def test_quote_pages_carry_open_graph_and_twitter_tags_in_the_raw_html(tmp_path):
    pages = {"live": ["quote-page.js", "/q/cav1"], "expired": ["quote-page.js", "/q/gone"], "unknown": ["quote-page.js", "/q/nope1"]}
    d = _run(tmp_path, pages)["pages"]
    live = d["live"]["body"]                                        # fetched with a Slackbot user agent: no JavaScript ran
    assert _meta(live, "og:title") == "Cavalier Diamond Options — Cushions"
    assert _meta(live, "og:description") == "3 diamonds selected — Cushions"
    assert _meta(live, "og:image") == "https://quote.alldiamondeverything.com/og/q/cav1.jpg"
    assert _meta(live, "og:image:width") == "1200" and _meta(live, "og:image:height") == "630"
    assert _meta(live, "og:image:type") == "image/jpeg"
    assert _meta(live, "twitter:card") == "summary_large_image"
    assert _meta(live, "twitter:image") == _meta(live, "og:image") and _meta(live, "twitter:title") == _meta(live, "og:title")
    assert _meta(live, "og:url") == "https://quote.alldiamondeverything.com/q/cav1"
    assert "<title>Cavalier Diamond Options — Cushions</title>" in live
    assert live.count('property="og:image"') == 1 and 'name="twitter:card" content="summary"' not in live
    assert "price" not in _meta(live, "og:description").lower()
    gone = d["expired"]["body"]                                     # expired: neutral text, same image URL (neutral card)
    assert _meta(gone, "og:title") == "Diamond Options" and "Cavalier" not in _meta(gone, "og:description")
    assert _meta(gone, "og:image") == "https://quote.alldiamondeverything.com/og/q/gone.jpg"
    assert _meta(gone, "twitter:card") == "summary_large_image"
    nope = d["unknown"]["body"]                                     # unknown quote: still a valid neutral preview
    assert _meta(nope, "og:title") == "Diamond Options" and _meta(nope, "og:image").endswith("/og/q/nope1.jpg")


def test_preview_images_render_for_each_case(tmp_path):
    imgs = {k: f"/og/q/{k}.jpg" for k in ("cav1", "pd01", "zed1", "bell", "noim", "vend", "gone", "nope1")}
    imgs.update({"neutral": "/og/neutral.jpg", "c_nash": "/og/c/nash-jewellers.jpg", "c_zed": "/og/c/zed-and-co.jpg",
                 "c_nobody": "/og/c/nobody.jpg"})
    d = _run(tmp_path, images=imgs)["images"]
    im = {k: _img(v) for k, v in d.items()}
    for k in ("cav1", "pd01", "zed1", "bell"):                      # built-in and in-app clients with a stone photo
        assert _stone_blue(im[k]) > 300, k
    assert _stone_blue(im["noim"]) == 0 and _stone_blue(im["vend"]) == 0     # no image / vendor URL: clean card, no photo
    assert _stone_blue(im["gone"]) == 0 and _stone_blue(im["nope1"]) == 0
    assert d["gone"]["b64"] == d["neutral"]["b64"] == d["nope1"]["b64"] == d["c_nobody"]["b64"]   # expired / unknown = neutral
    assert d["cav1"]["b64"] != d["zed1"]["b64"] != d["bell"]["b64"]                # each client looks different
    assert d["c_nash"]["b64"] != d["c_zed"]["b64"] and d["c_nash"]["b64"] != d["neutral"]["b64"]
    assert d["noim"]["headers"]["Cache-Control"].startswith("public, max-age=3600")
    # neutral card: only the Pure Carbon logo (nothing else bright outside the centre)
    n = im["neutral"]
    assert max(sum(n.getpixel((x, y))) for x in range(60, 300, 20) for y in range(60, 570, 20)) < 200


def test_expiring_quote_image_is_not_cached_past_its_expiry(tmp_path):
    d = _run(tmp_path, images={"soon": "/og/q/soon.jpg", "cav": "/og/q/cav1.jpg"})["images"]
    cdn = int(re.search(r"s-maxage=(\d+)", d["soon"]["headers"]["Netlify-CDN-Cache-Control"]).group(1))
    assert 60 <= cdn <= 600
    assert int(re.search(r"max-age=(\d+)", d["soon"]["headers"]["Cache-Control"]).group(1)) <= 600
    assert "s-maxage=86400" in d["cav"]["headers"]["Netlify-CDN-Cache-Control"]


def test_summary_text_and_no_private_details_on_the_card(tmp_path):
    out = _run(tmp_path)["svgs"]
    assert out["cav1"] == "3 cushion diamonds · 1.52–2.10 ct"
    assert out["bell"] == "3 diamonds · oval, round, pear · 1.02–3.01 ct"
    assert out["pd01"] == "1 round diamond · 1.00 ct"
    js = r"""
      const P = require('./netlify/functions/lib/preview.js');
      const svg = P.cardSvg({ name: 'Cavalier', summary: '3 cushion diamonds · 1.52–2.10 ct', logo: 'data:image/png;base64,AA', pcgLogo: 'data:image/png;base64,AA', photo: 'data:image/jpeg;base64,AA' });
      process.stdout.write(JSON.stringify([...svg.matchAll(/<text[^>]*>([^<]*)<\/text>/g)].map((m) => m[1])));
    """
    texts = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, cwd=ROOT).stdout)
    assert texts == ["Cavalier", "3 cushion diamonds · 1.52–2.10 ct", "PURE CARBON GROUP"]     # nothing else on the card
    src = open(os.path.join(ROOT, "netlify", "functions", "og-image.js")).read()
    assert not re.search(r"\b(price|cert_last4|pdf_url|orig_filename)\b", src)                  # those fields are never read


def test_share_pages_use_the_same_card_style(tmp_path):
    pages = {"nash": ["customer-page.js", "/c/nash-jewellers/abc123"], "zed": ["customer-page.js", "/c/zed-and-co/abc123"],
             "nobody": ["customer-page.js", "/c/nobody/abc123"]}
    d = _run(tmp_path, pages)["pages"]
    for k, slug in (("nash", "nash-jewellers"), ("zed", "zed-and-co")):
        h = d[k]["body"]
        assert _meta(h, "og:image") == f"https://quote.alldiamondeverything.com/og/c/{slug}.jpg"
        assert _meta(h, "og:image:width") == "1200" and _meta(h, "og:image:height") == "630"
        assert _meta(h, "twitter:card") == "summary_large_image" and 'content="summary"' not in h
    assert _meta(d["nash"]["body"], "og:title") == "Nash Jewellers — Diamond Selection"
    assert _meta(d["nobody"]["body"], "og:image").endswith("/og/neutral.jpg")
    assert "Nash Jewellers" not in d["zed"]["body"] and "Zed" not in d["nash"]["body"]


def test_netlify_routes_the_preview_images():
    toml = open(os.path.join(ROOT, "netlify.toml")).read()
    assert 'from = "/og/*"' in toml and "functions/og-image/:splat" in toml
