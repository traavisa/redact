// Open Graph + Twitter card tags for link previews (Slack, WhatsApp, iMessage, email...). They go
// into the page's initial HTML because those crawlers don't run JavaScript.
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function origin(event) {
  const h = (event && event.headers) || {};
  const host = String(h['x-forwarded-host'] || h.host || '').split(',')[0].trim();
  return /^[a-z0-9.-]+(:\d+)?$/i.test(host) ? `https://${host}` : 'https://quote.alldiamondeverything.com';
}

// { title, description, image (absolute URL), url (absolute), siteName }
function ogTags(o) {
  const t = [
    ['property', 'og:type', 'website'],
    ['property', 'og:title', o.title],
    ['property', 'og:description', o.description],
    ['property', 'og:image', o.image],
    ['property', 'og:image:secure_url', o.image],
    ['property', 'og:image:type', 'image/jpeg'],
    ['property', 'og:image:width', '1200'],
    ['property', 'og:image:height', '630'],
    ['property', 'og:image:alt', o.alt || o.title],
    ['name', 'twitter:card', 'summary_large_image'],
    ['name', 'twitter:title', o.title],
    ['name', 'twitter:description', o.description],
    ['name', 'twitter:image', o.image],
  ];
  if (o.url) t.splice(1, 0, ['property', 'og:url', o.url]);
  if (o.siteName) t.splice(1, 0, ['property', 'og:site_name', o.siteName]);
  return t.map(([k, n, v]) => `<meta ${k}="${n}" content="${esc(v)}" />`).join('\n  ');
}

module.exports = { ogTags, origin, esc };
