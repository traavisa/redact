// Share pages carry a capture's folder id. For a capture that may not be shown (first
// build), this returns what to show instead: the stone's re-captured frames if it has been
// redone ({ spin }), else its original viewer as our own /v/ link ({ token }).
// The folder id is random (128 bits) and only known to holders of the share link.
const { trustedRow, rowForSpin } = require('./lib/spin');

exports.handler = async function (event) {
  const json = (code, body) => ({
    statusCode: code,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex' },
    body: JSON.stringify(body),
  });
  if (event.httpMethod !== 'GET') return json(405, { error: 'Method not allowed' });
  const id = String((event.queryStringParameters && event.queryStringParameters.id) || '');
  if (!/^[a-f0-9]{32}$/.test(id)) return json(400, { error: 'Bad request' });
  let row;
  try { row = await rowForSpin(id); } catch (e) { return json(502, { error: 'Unavailable' }); }
  if (!row) return json(404, { error: 'Not found' });
  const spin = trustedRow(row);
  if (spin) return json(200, { spin });
  if (/^[a-z2-9]{8,32}$/.test(String(row.token || ''))) return json(200, { token: row.token });
  return json(404, { error: 'Not found' });
};
