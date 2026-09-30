// /client-logo/<slug>                 -> the client's logo as an image, from our own domain.
// /client-logo/<slug>/icon-<n>.png    -> square favicon / home-screen icon (n = 32, 180 or 512).
// Order: built-in logos, then clients added in the app, then Pure Carbon Group. A client whose
// logo can't be turned into an icon (unsupported or corrupt image) gets the Pure Carbon icon.
const { logoFor, lookup, slugOf, FALLBACK } = require('./lib/clients');
const { SIZES, makeIcon } = require('./lib/icons');

exports.handler = async function (event) {
  const parts = String(event.path || '').split('/').filter(Boolean);
  const at = parts.lastIndexOf('client-logo');
  const rest = at >= 0 ? parts.slice(at + 1) : parts.slice(-1);
  const icon = rest.length > 1 ? /^icon-(\d+)\.png$/.exec(rest[rest.length - 1]) : null;
  const slug = (rest[0] || '').toLowerCase().replace(/\.(png|jpe?g|webp)$/, '');

  if (icon) {
    const size = Number(icon[1]);
    if (!SIZES.includes(size)) return { statusCode: 404, body: 'Not found' };
    const own = await lookup(slug);
    let png = null, used = own;
    if (own) { try { png = makeIcon(own.logo, size); } catch (e) { console.warn(`icon for "${slug}" unusable: ${e.message}`); } }
    if (!png) {                                     // no logo, or one we can't use: Pure Carbon
      used = await lookup(slugOf(FALLBACK));
      try { png = used && makeIcon(used.logo, size); } catch (e) { png = null; }
    }
    if (!png) return { statusCode: 404, body: 'Not found' };
    return {
      statusCode: 200, isBase64Encoded: true,
      headers: { 'Content-Type': 'image/png', 'Cache-Control': 'public, max-age=300',
                 'X-Icon-Source': used === own ? 'client' : 'fallback' },
      body: png.toString('base64'),
    };
  }

  const hit = await logoFor(slug);
  const m = hit && /^data:(image\/[a-z+]+);base64,(.+)$/.exec(hit.logo);
  if (!m) return { statusCode: 404, body: 'Not found' };
  return {
    statusCode: 200,
    isBase64Encoded: true,
    headers: { 'Content-Type': m[1], 'Cache-Control': 'public, max-age=300' },
    body: m[2],
  };
};
