// Viewer links /v/<token>: returns the embed URL stored for ONE token.
// Tokens are random (never derived from the URL); the table has no public access,
// so only this function (with the server-side service key) can read it.
const TOKEN_RE = /^[a-z2-9]{8,32}$/;

exports.handler = async function (event) {
  const json = (code, body) => ({
    statusCode: code,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex' },
    body: JSON.stringify(body),
  });
  if (event.httpMethod !== 'GET') return json(405, { error: 'Method not allowed' });
  const t = String((event.queryStringParameters && event.queryStringParameters.t) || '');
  if (!TOKEN_RE.test(t)) return json(400, { error: 'Bad request' });

  const KEY = process.env.SUPABASE_SERVICE_KEY;
  const lookup = async (cols) => {
    const res = await fetch(
      `${process.env.SUPABASE_URL}/rest/v1/media_links?token=eq.${t}&select=${cols}&limit=1`,
      { headers: { apikey: KEY, Authorization: `Bearer ${KEY}` } }
    );
    return res.ok ? await res.json() : null;
  };
  let rows;
  try {
    // Before the spin columns exist (SQL update not run yet) only vendor_url can be read
    rows = (await lookup('vendor_url,spin_id,spin_frames')) || (await lookup('vendor_url')) || [];
  } catch (e) { return json(502, { error: 'Unavailable' }); }
  const row = rows && rows[0];
  if (!row) return json(404, { error: 'Not found' });
  // Captured 360 frames: the viewer plays our own copies and never learns the vendor URL
  const id = String(row.spin_id || ''), n = Number(row.spin_frames);
  if (/^[a-f0-9]{32}$/.test(id) && Number.isInteger(n) && n >= 2 && n <= 720) return json(200, { spin: { id, n } });
  const url = row.vendor_url;
  if (!url || !/^https?:\/\//i.test(url)) return json(404, { error: 'Not found' });
  return json(200, { url });
};
