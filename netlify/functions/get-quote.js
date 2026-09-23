// Returns ONE quote, stripped to what the client-facing page needs.
// Reads Supabase with the server-side service key (never sent to the browser),
// so the quotes table can stay locked to the public.
const SAFE_STONE_FIELDS = [
  'cert_last4', 'cert_type', 'video_url', 'pdf_url',
  'price', 'currency', 'price_type', 'cert_data',
];

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
  const stones = expired ? [] : (q.stones || []).map((s) => {
    const out = {};
    SAFE_STONE_FIELDS.forEach((k) => { if (s[k] !== undefined) out[k] = s[k]; });
    const ct = certTypeOf(s);
    if (ct) out.cert_type = ct;
    return out;
  });

  return json(200, { client: q.client, expires_at: q.expires_at, stones });
};
