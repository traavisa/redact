// Returns ONE encrypted customer share by its short code.
// The data is still locked: the key is only in the customer's link after "#".
// Expired shares are refused and deleted.
exports.handler = async function (event) {
  const json = (code, body) => ({
    statusCode: code,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
    body: JSON.stringify(body),
  });
  const id = String(event.path || '').split('/').filter(Boolean).pop() || '';
  if (!/^[a-z0-9]{6,16}$/.test(id)) return json(400, { error: 'Bad request' });

  const URL_BASE = `${process.env.SUPABASE_URL}/rest/v1/share_links`;
  const KEY = process.env.SUPABASE_SERVICE_KEY;
  const headers = { apikey: KEY, Authorization: `Bearer ${KEY}` };

  const res = await fetch(`${URL_BASE}?id=eq.${id}&select=blob,expires_at&limit=1`, { headers });
  const rows = res.ok ? await res.json() : [];
  if (!rows || !rows[0]) return json(404, { error: 'Not found' });

  if (new Date(rows[0].expires_at) < new Date()) {
    try { await fetch(`${URL_BASE}?id=eq.${id}`, { method: 'DELETE', headers }); } catch (e) {}
    return json(410, { error: 'Expired' });
  }
  return json(200, { d: rows[0].blob, expires_at: rows[0].expires_at });
};
