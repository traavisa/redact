// Link-preview image (1200x630 JPEG) served from our own domain:
//   /og/q/<quote id>.jpg  quote page: client logo, first stone still (our copy), "3 cushion diamonds · 1.52–2.10 ct"
//   /og/c/<client>.jpg    share page: client logo and name (share data is encrypted, so no stones)
//   /og/neutral.jpg       Pure Carbon logo only (also used for expired / unknown quotes)
// No prices, report numbers or vendor information ever go into the card. Never errors: any
// problem gives the neutral card.
const { slugOf, lookup, FALLBACK } = require('./lib/clients');
const { summarize, logoUri, photoUri, renderCard, IMG_W, H } = require('./lib/preview');

const ID_RE = /^[a-z0-9]{4,32}$/i;
const STILL_RE = /^[a-f0-9]{32}\.(jpg|png)$/;                 // one re-hosted still (never a 360 frame)
const MAX_IMG = 6 * 1024 * 1024;

async function getJson(url) {
  const KEY = process.env.SUPABASE_SERVICE_KEY;
  const r = await fetch(url, { headers: { apikey: KEY, Authorization: `Bearer ${KEY}` } });
  return r.ok ? r.json() : null;
}

// The first stone that has one of our own still images (image_url, or a video_url that is a still).
function stillOf(stones) {
  const base = 'https://quote.alldiamondeverything.com/media/';
  for (const s of stones || []) {
    for (const u of [s && s.image_url, s && s.video_url]) {
      const f = String(u || '').startsWith(base) ? String(u).slice(base.length) : '';
      if (STILL_RE.test(f)) return f;
    }
  }
  return null;
}

async function fetchStill(file) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 4000);
  try {
    const r = await fetch(`${process.env.SUPABASE_URL}/storage/v1/object/public/quote-media/${file}`, { signal: ctl.signal });
    if (!r.ok) return null;
    const buf = Buffer.from(await r.arrayBuffer());
    return buf.length && buf.length <= MAX_IMG ? buf : null;
  } catch (e) { return null; } finally { clearTimeout(timer); }
}

async function pcgLogo() {
  const hit = await lookup(slugOf(FALLBACK));
  return hit ? logoUri(hit.logo) : null;
}

async function clientCard(name, summary, stones) {
  const hit = await lookup(slugOf(name)) || await lookup(slugOf(FALLBACK));
  const file = stillOf(stones);
  const buf = file ? await fetchStill(file) : null;
  return {
    name: name || 'Pure Carbon Group', summary,
    logo: hit ? logoUri(hit.logo) : null, pcgLogo: await pcgLogo(),
    photo: buf ? photoUri(buf, IMG_W, H) : null,
  };
}

exports.handler = async function (event) {
  const parts = String(event.path || '').split('/').filter(Boolean);
  const at = Math.max(parts.lastIndexOf('og-image'), parts.lastIndexOf('og'));
  const rest = at >= 0 ? parts.slice(at + 1) : parts;
  const kind = rest[0], key = String(rest[1] || '').replace(/\.jpe?g$/i, '');
  let card = null, maxAge = 3600, cdn = 86400;
  try {
    if (kind === 'q' && ID_RE.test(key)) {
      const rows = await getJson(`${process.env.SUPABASE_URL}/rest/v1/quotes?id=eq.${encodeURIComponent(key)}&select=client,stones,expires_at&limit=1`);
      const q = rows && rows[0];
      if (q) {
        const left = new Date(q.expires_at) - Date.now();
        if (left > 0) {
          card = await clientCard(q.client, summarize(q.stones).text, q.stones);
          cdn = Math.max(60, Math.min(cdn, Math.floor(left / 1000)));      // never cached past its expiry
          maxAge = Math.min(maxAge, cdn);
        }
      }
    } else if (kind === 'c' && key) {
      const hit = await lookup(key.toLowerCase());
      if (hit) card = await clientCard(hit.name, 'Diamonds selected for you', []);
    }
  } catch (e) { console.warn('og-image: ' + e.message); card = null; }
  let jpg;
  try {
    if (!card) { card = { neutral: true, pcgLogo: await pcgLogo() }; maxAge = 300; cdn = 300; }
    jpg = await renderCard(card);
  } catch (e) {
    console.warn('og-image render failed: ' + e.message);
    try { jpg = await renderCard({ neutral: true, pcgLogo: null }); maxAge = 60; cdn = 60; } catch (e2) { return { statusCode: 500, body: 'Error' }; }
  }
  return {
    statusCode: 200, isBase64Encoded: true,
    headers: { 'Content-Type': 'image/jpeg', 'Cache-Control': `public, max-age=${maxAge}`,
               'Netlify-CDN-Cache-Control': `public, s-maxage=${cdn}, stale-while-revalidate=86400`, 'X-Robots-Tag': 'noindex' },
    body: jpg.toString('base64'),
  };
};
