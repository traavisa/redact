// Link generator (password-protected landing page): stores a video/360 URL under a
// random token and returns https://video.alldiamondeverything.com/v/<token>.
// The caller must hold the signed pass issued by verify-password.
const crypto = require('crypto');
const { validPass } = require('./lib/pass');

const ALPHABET = 'abcdefghijkmnpqrstuvwxyz23456789';
const VIEWER_LINK_BASE = 'https://video.alldiamondeverything.com/v/';

function newToken(n = 12) {
  const bytes = crypto.randomBytes(n);
  let t = '';
  for (let i = 0; i < n; i++) t += ALPHABET[bytes[i] % ALPHABET.length];   // 256 % 32 == 0: unbiased
  return t;
}

exports.handler = async function (event) {
  const json = (code, body) => ({
    statusCode: code,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
    body: JSON.stringify(body),
  });
  if (event.httpMethod !== 'POST') return json(405, { error: 'Method not allowed' });

  let body;
  try { body = JSON.parse(event.body || '{}'); } catch (e) { return json(400, { error: 'Bad request' }); }
  if (!validPass(body.pass)) return json(401, { error: 'Unauthorized' });
  const url = String(body.url || '').trim();
  if (!/^https?:\/\/[^\s"'<>]+$/i.test(url) || url.length > 2000) return json(400, { error: 'Bad URL' });

  const REST = `${process.env.SUPABASE_URL}/rest/v1/media_links`;
  const KEY = process.env.SUPABASE_SERVICE_KEY;
  const headers = { apikey: KEY, Authorization: `Bearer ${KEY}` };

  // Same URL again: reuse its token
  try {
    const res = await fetch(`${REST}?vendor_url=eq.${encodeURIComponent(url)}&select=token&limit=1`, { headers });
    const rows = res.ok ? await res.json() : [];
    if (rows && rows[0] && rows[0].token) return json(200, { link: VIEWER_LINK_BASE + rows[0].token });
  } catch (e) { /* fall through and create one */ }

  for (let attempt = 0; attempt < 3; attempt++) {
    const token = newToken();
    const res = await fetch(REST, {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json', Prefer: 'return=minimal' },
      body: JSON.stringify({ token, vendor_url: url }),
    });
    if (res.ok) return json(200, { link: VIEWER_LINK_BASE + token });
    if (res.status !== 409) break;
  }
  return json(500, { error: 'Could not save' });
};
