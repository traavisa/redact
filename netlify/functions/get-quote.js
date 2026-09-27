// Returns ONE quote, stripped to what the client-facing page needs.
// Reads Supabase with the server-side service key (never sent to the browser),
// so the quotes table can stay locked to the public.
const SAFE_STONE_FIELDS = [
  'cert_last4', 'cert_type', 'video_url', 'image_url', 'pdf_url',
  'price', 'currency', 'price_type', 'cert_data', 'spin',
];

// Media may only point at our own addresses: re-hosted files (/media/), opaque viewer
// links (/v/<token>) or, for older quotes, the legacy viewer link. Anything else is dropped.
const MEDIA_BASE = 'https://quote.alldiamondeverything.com/media/';
const VIEWER_LINK_BASE = 'https://video.alldiamondeverything.com/v/';
const LEGACY_VIEWER_BASE = 'https://video.alldiamondeverything.com/?u=';
const MEDIA_FILE_RE = /^[a-f0-9]{32}(\.(jpg|png|mp4|webm)|\/\d{3}\.(jpg|webp))$/;   // files, or one captured 360 frame
function safeVideo(u) {
  u = String(u || '');
  if (u.startsWith(MEDIA_BASE)) return MEDIA_FILE_RE.test(u.slice(MEDIA_BASE.length)) ? u : undefined;
  if (u.startsWith(VIEWER_LINK_BASE)) return /^[a-z2-9]{8,32}$/.test(u.slice(VIEWER_LINK_BASE.length)) ? u : undefined;
  if (u.startsWith(LEGACY_VIEWER_BASE)) return u;
  return undefined;
}
// Captured 360 frames: our own folder /media/<32 hex>/000.webp … (v5+; 000.jpg … for v4). Only captures
// the rule in lib/spin.js trusts are served; others fall back to the original viewer.
const { trustedSpin, rowForSpin, spinsForClient } = require('./lib/spin');
const FRAME_IMAGE_RE = /^[a-f0-9]{32}\/\d{3}\.(jpg|webp)$/;
const VIEWER_TOKEN_RE = /^[a-z2-9]{8,32}$/;
// Certificates: our own /certs/ copies (random names), or the older uploads in our own
// storage. Anything else is dropped.
const CERT_BASE = 'https://quote.alldiamondeverything.com/certs/';
const LEGACY_CERT_BASE = 'https://srlbevzrkovruyerixdi.supabase.co/storage/v1/object/public/certificates/';
function safePdf(u) {
  u = String(u || '');
  if (u.startsWith(CERT_BASE)) return /^[a-f0-9]{32}\.pdf$/.test(u.slice(CERT_BASE.length)) ? u : undefined;
  if (u.startsWith(LEGACY_CERT_BASE)) return /^[A-Za-z0-9._-]+$/.test(u.slice(LEGACY_CERT_BASE.length)) ? u : undefined;
  return undefined;
}
function safeImage(u) {
  u = String(u || '');
  return u.startsWith(MEDIA_BASE) && MEDIA_FILE_RE.test(u.slice(MEDIA_BASE.length)) ? u : undefined;
}

function certTypeOf(stone) {
  if (stone.cert_type) return stone.cert_type;
  const hint = String(stone.orig_filename || '').toUpperCase();   // older manual quotes
  if (hint.includes('GIA')) return 'GIA';
  if (hint.includes('IGI')) return 'IGI';
  return undefined;                                                 // page falls back to reading the PDF
}

exports.handler = async function (event) {
  const id = event.path.split('/').filter(Boolean).pop();
  const json = (code, body) => ({
    statusCode: code,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
    body: JSON.stringify(body),
  });
  if (!id || !/^[a-z0-9]{4,32}$/i.test(id)) return json(400, { error: 'Missing ID' });

  const KEY = process.env.SUPABASE_SERVICE_KEY;
  const res = await fetch(
    `${process.env.SUPABASE_URL}/rest/v1/quotes?id=eq.${encodeURIComponent(id)}&select=client,stones,expires_at&limit=1`,
    { headers: { apikey: KEY, Authorization: `Bearer ${KEY}` } }
  );
  const rows = res.ok ? await res.json() : [];
  if (!rows || !rows[0]) return json(404, { error: 'Quote not found' });

  const q = rows[0];
  const expired = new Date(q.expires_at) < new Date();
  // Kill switch: off, or "test" and this isn't the test client's quote = as if capture were off
  const spinsOn = spinsForClient(q.client);
  const stones = expired ? [] : await Promise.all((q.stones || []).map(async (s) => {
    const out = {};
    SAFE_STONE_FIELDS.forEach((k) => { if (s[k] !== undefined) out[k] = s[k]; });
    if ('video_url' in out) { const v = safeVideo(out.video_url); if (v) out.video_url = v; else delete out.video_url; }
    if ('image_url' in out) { const v = safeImage(out.image_url); if (v) out.image_url = v; else delete out.image_url; }
    if ('pdf_url' in out) { const v = safePdf(out.pdf_url); if (v) out.pdf_url = v; else delete out.pdf_url; }
    // Kill switch off for this quote: no captured frame is ever served, not even as a still image
    if (!spinsOn && out.image_url && FRAME_IMAGE_RE.test(out.image_url.slice(MEDIA_BASE.length))) delete out.image_url;
    if ('spin' in out) {
      const v = spinsOn ? trustedSpin(out.spin) : null;
      if (v) { out.spin = v; delete out.video_url; }
      else {
        // A capture that must not be shown: never its frames (not even as the still image).
        const bad = String((out.spin && out.spin.id) || '');
        delete out.spin;
        if (bad && out.image_url && out.image_url.startsWith(MEDIA_BASE + bad + '/')) delete out.image_url;
        // Fall back to the original viewer: our /v/ link kept on the stone, else the
        // media_links row the capture was recorded on (nothing there is ever deleted).
        let ref = s.media_ref && safeVideo(s.media_ref);
        if (!ref && /^[a-f0-9]{32}$/.test(bad)) {
          const row = await rowForSpin(bad).catch(() => null);
          if (row && VIEWER_TOKEN_RE.test(String(row.token || ''))) ref = VIEWER_LINK_BASE + row.token;
        }
        if (ref && !out.video_url) out.video_url = ref;
      }
    }
    const ct = certTypeOf(s);
    if (ct) out.cert_type = ct;
    return out;
  }));

  return json(200, { client: q.client, expires_at: q.expires_at, stones });
};
