// /client-logo/<slug> -> the client's logo as an image, from our own domain.
// Order: clients added in the app, then the built-in logos, then Pure Carbon Group.
const { logoFor } = require('./lib/clients');

exports.handler = async function (event) {
  const slug = String(event.path || '').split('/').filter(Boolean).pop() || '';
  const hit = await logoFor(slug.toLowerCase().replace(/\.(png|jpe?g|webp)$/, ''));
  const m = hit && /^data:(image\/[a-z+]+);base64,(.+)$/.exec(hit.logo);
  if (!m) return { statusCode: 404, body: 'Not found' };
  return {
    statusCode: 200,
    isBase64Encoded: true,
    headers: { 'Content-Type': m[1], 'Cache-Control': 'public, max-age=300' },
    body: m[2],
  };
};
