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
const { iconTags } = require('./lib/icons');
const { ogTags, origin } = require('./lib/og');

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

  // Link preview (server-side): same 1200x630 card style as the quote pages, this client's logo and name
  const base = origin(event);
  let title = 'Diamond Selection';
  let meta = ogTags({ title, description: 'Diamonds selected for you.', image: `${base}/og/neutral.jpg`, siteName: 'Pure Carbon Group' });

  if (store) {
    title = `${store.name} \u2014 Diamond Selection`;
    meta = ogTags({ title, description: `Diamonds selected for you by ${store.name}.`, siteName: store.name,
      image: `${base}/og/c/${encodeURIComponent(slug)}.jpg`, alt: `${store.name} \u2014 Diamond Selection` });
    html = html
      .replace('const STORE_LOGO = null;', `const STORE_LOGO = ${JSON.stringify(store.logo)};`)
      .replace('const STORE_NAME = null;', `const STORE_NAME = ${JSON.stringify(store.name).replace(/</g, '\\u003c')};`);
  }

  // Favicon + home-screen icon: this client's logo (square, from our domain); Pure Carbon's if unknown
  html = html.replace(/<link rel="icon" id="favicon"[^>]*>/, iconTags(store ? slug : ''));

  html = html.replace(
    '<title>Diamond Selection</title>',
    `<title>${escapeHtml(title)}</title>\n  ${meta}`
  );

  return {
    statusCode: 200,
    headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'public, max-age=300' },
    body: html,
  };
};
