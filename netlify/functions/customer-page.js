// Serves the customer share page at /c/<store>#<data>.
// Only the store name in the path reaches this server. Everything after "#"
// (the diamonds, specs and any price) stays in the customer's browser.
// No database, no quote data, no logging.
//
// The store's logo is taken from the CLIENT_LOGOS list in quote.html, so a new
// client logo only ever needs adding there. Only that ONE logo is put in the
// page, so the customer never sees your other clients' names.
const fs = require('fs');
const path = require('path');

// Optional link-preview images (WhatsApp / iMessage), from the /logos folder
const PREVIEW_IMAGES = {
  'pure-carbon-group': 'Pure Carbon Group.png', 'cavalier': 'Cavalier.png', 'foe-and-dear': 'Foe & Dear.png',
  'harlings': 'Harlings.png', 'rodan': 'Rodan.png', 'nfr': 'NFR.png', 'nash-jewellers': 'Nash.png',
  'janinas': 'janinas.jpg', 'ijl': 'IJL2.png', 'gem-by-carati': 'gem.jpg', 'vena-nova': 'Vena.jpg',
  'touch-of-gold': 'TOG.png',
};

function slugOf(name) {
  return String(name || '').toLowerCase().replace(/&/g, 'and').replace(/['’]/g, '')
    .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
}
function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

let STORES = null;   // slug -> { name, logo }
function stores() {
  if (STORES) return STORES;
  STORES = {};
  try {
    const q = fs.readFileSync(path.join(__dirname, '..', '..', 'quote.html'), 'utf8');
    const start = q.indexOf('const CLIENT_LOGOS = {');
    const block = q.slice(start, q.indexOf('\n};', start));
    const re = /^\s*"((?:[^"\\]|\\.)+)":\s*"(data:image\/[a-z+]+;base64,[A-Za-z0-9+/=]+)"/gm;
    let m;
    while ((m = re.exec(block))) {
      STORES[slugOf(m[1])] = { name: m[1], logo: m[2] };
    }
  } catch (e) { /* page still works without a logo */ }
  return STORES;
}

exports.handler = async function (event) {
  let html = fs.readFileSync(path.join(__dirname, '..', '..', 'customer.html'), 'utf8');
  const slug = (String(event.path || '').split('/').filter(Boolean).pop() || '').toLowerCase();
  const store = stores()[slug];

  let title = 'Diamond Selection';
  let meta = `<meta property="og:title" content="Diamond Selection" />
  <meta property="og:description" content="Diamonds selected for you." />`;

  if (store) {
    title = `${store.name} — Diamond Selection`;
    meta = `<meta property="og:title" content="${escapeHtml(title)}" />
  <meta property="og:description" content="${escapeHtml(`Diamonds selected for you by ${store.name}.`)}" />
  <meta property="og:site_name" content="${escapeHtml(store.name)}" />`;
    const host = (event.headers && (event.headers['x-forwarded-host'] || event.headers.host)) || '';
    if (host && PREVIEW_IMAGES[slug]) {
      meta += `\n  <meta property="og:image" content="${escapeHtml(`https://${host}/logos/${encodeURIComponent(PREVIEW_IMAGES[slug])}`)}" />`;
    }
    html = html
      .replace('<link rel="icon" id="favicon" href="data:,">', `<link rel="icon" id="favicon" href="${store.logo}">`)
      .replace('const STORE_LOGO = null;', `const STORE_LOGO = ${JSON.stringify(store.logo)};`);
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
