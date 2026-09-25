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
  let rows = [];
  try {
    const res = await fetch(
      `${process.env.SUPABASE_URL}/rest/v1/media_links?token=eq.${t}&select=vendor_url&limit=1`,
      { headers: { apikey: KEY, Authorization: `Bearer ${KEY}` } }
    );
    rows = res.ok ? await res.json() : [];
  } catch (e) { return json(502, { error: 'Unavailable' }); }
  const url = rows && rows[0] && rows[0].vendor_url;
  if (!url || !/^https?:\/\//i.test(url)) return json(404, { error: 'Not found' });
  return json(200, { url });
};
