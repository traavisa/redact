// Stores ONE encrypted customer share and returns a short code for it.
// The associate's phone encrypts the share before sending it here; the key to
// unlock it stays in the part of the link after "#", which never reaches any
// server. So this function (and the database) only ever sees unreadable data.
// Nothing else is stored: no IP address, no store, no prices, no recipient.
const crypto = require('crypto');

const MAX_LEN = 16000;          // plenty for ~30 diamonds; stops abuse
const DAYS = 30;
const ALPHABET = 'abcdefghijkmnpqrstuvwxyz23456789';

function newId(n = 10) {
  const bytes = crypto.randomBytes(n);
  let id = '';
  for (let i = 0; i < n; i++) id += ALPHABET[bytes[i] % ALPHABET.length];
  return id;
}

exports.handler = async function (event) {
  const json = (code, body) => ({
    statusCode: code,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
    body: JSON.stringify(body),
  });
  if (event.httpMethod !== 'POST') return json(405, { error: 'Method not allowed' });

  // Only accept requests from our own pages
  const origin = (event.headers && (event.headers.origin || event.headers.Origin)) || '';
  const host = (event.headers && (event.headers['x-forwarded-host'] || event.headers.host)) || '';
  if (origin && host) {
    try { if (new URL(origin).host !== host) return json(403, { error: 'Forbidden' }); } catch (e) { return json(403, { error: 'Forbidden' }); }
  }

  let d;
  try { d = JSON.parse(event.body || '{}').d; } catch (e) { return json(400, { error: 'Bad request' }); }
  if (typeof d !== 'string' || d.length < 40 || d.length > MAX_LEN || !/^[A-Za-z0-9_-]+$/.test(d)) {
    return json(400, { error: 'Bad request' });
  }

  const URL_BASE = `${process.env.SUPABASE_URL}/rest/v1/share_links`;
  const KEY = process.env.SUPABASE_SERVICE_KEY;
  const headers = { apikey: KEY, Authorization: `Bearer ${KEY}`, 'Content-Type': 'application/json', Prefer: 'return=minimal' };
  const now = new Date();
  const expires_at = new Date(now.getTime() + DAYS * 864e5).toISOString();

  // Tidy up: remove any shares that have already expired
  try { await fetch(`${URL_BASE}?expires_at=lt.${encodeURIComponent(now.toISOString())}`, { method: 'DELETE', headers }); } catch (e) {}

  for (let attempt = 0; attempt < 3; attempt++) {
    const id = newId();
    const res = await fetch(URL_BASE, { method: 'POST', headers, body: JSON.stringify({ id, blob: d, expires_at }) });
    if (res.ok) return json(200, { id, expires_at });
    if (res.status !== 409) break;       // 409 = code already used; try another
  }
  return json(500, { error: 'Could not save' });
};
