// Serves the customer share page at /c/<store>/<code>#<key> (short, encrypted)
// and /c/<store>#<data> (older long links). Only the store and the short code
// reach this server. The key or data after "#" stays in the customer's browser.
// No quote data, no logging; the only database read is the client list (name + logo).
//
// The store's logo comes from the app's client list in the database (clients added with
// "Add a new client"), else from the CLIENT_LOGOS list in quote.html (see lib/clients.js).
// Only that ONE logo is put in the page, so the customer never sees your other clients' names.
const fs = require('fs');
const path = require('path');

const { slugOf, lookup } = require('./lib/clients');

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

exports.handler = async function (event) {
  let html = fs.readFileSync(path.join(__dirname, '..', '..', 'customer.html'), 'utf8');
  // Path is /c/<store> or /c/<store>/<code>; pick the segment that is a known store
  const parts = String(event.path || '').toLowerCase().split('/').filter(Boolean);
  let slug = '', store = null;
  for (const p of parts) {                           // clients added in the app first, then the built-in ones
    const hit = await lookup(p);
    if (hit) { slug = p; store = hit; break; }
  }

  let title = 'Diamond Selection';
  let meta = `<meta property="og:title" content="Diamond Selection" />
  <meta property="og:description" content="Diamonds selected for you." />`;

  if (store) {
    title = `${store.name} — Diamond Selection`;
    meta = `<meta property="og:title" content="${escapeHtml(title)}" />
  <meta property="og:description" content="${escapeHtml(`Diamonds selected for you by ${store.name}.`)}" />
  <meta property="og:site_name" content="${escapeHtml(store.name)}" />`;
    const host = (event.headers && (event.headers['x-forwarded-host'] || event.headers.host)) || '';
    if (host) {                                      // link-preview image: this client's logo, from our own domain
      meta += `\n  <meta property="og:image" content="${escapeHtml(`https://${host}/client-logo/${slug}`)}" />`;
    }
    html = html
      .replace('<link rel="icon" id="favicon" href="data:,">', `<link rel="icon" id="favicon" href="${store.logo}">`)
      .replace('const STORE_LOGO = null;', `const STORE_LOGO = ${JSON.stringify(store.logo)};`)
      .replace('const STORE_NAME = null;', `const STORE_NAME = ${JSON.stringify(store.name).replace(/</g, '\\u003c')};`);
  }

  html = html.replace(
    '<title>Diamond Selection</title>',
    `<title>${escapeHtml(title)}</title>\n  ${meta}\n  <meta property="og:type" content="website" />\n  <meta name="twitter:card" content="summary" />`
  );

  return {
    statusCode: 200,
    headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'public, max-age=300' },
    body: html,
  };
};
