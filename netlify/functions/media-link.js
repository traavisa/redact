// Viewer links /v/<token>: returns the embed URL stored for ONE token.
// Tokens are random (never derived from the URL); the table has no public access,
// so only this function (with the server-side service key) can read it.
const TOKEN_RE = /^[a-z2-9]{8,32}$/;
const { trustedRow, mediaLinks } = require('./lib/spin');

exports.handler = async function (event) {
  const json = (code, body) => ({
    statusCode: code,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex' },
    body: JSON.stringify(body),
  });
  if (event.httpMethod !== 'GET') return json(405, { error: 'Method not allowed' });
  const t = String((event.queryStringParameters && event.queryStringParameters.t) || '');
  if (!TOKEN_RE.test(t)) return json(400, { error: 'Bad request' });

  let rows;
  try { rows = (await mediaLinks(`token=eq.${t}`, '&limit=1')) || []; } catch (e) { return json(502, { error: 'Unavailable' }); }
  const row = rows && rows[0];
  if (!row) return json(404, { error: 'Not found' });
  // Trusted captured frames: the viewer plays our own copies and never learns the vendor URL.
  // Untrusted captures (first build, under 24 frames) are ignored: the original viewer is used.
  const spin = trustedRow(row);
  if (spin) return json(200, { spin });
  const url = row.vendor_url;
  if (!url || !/^https?:\/\//i.test(url)) return json(404, { error: 'Not found' });
  return json(200, { url });
};
