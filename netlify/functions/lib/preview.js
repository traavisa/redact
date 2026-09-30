// Link-preview cards (Open Graph images), 1200x630 JPEG, in the quote page's dark / gold style.
// Text is set with resvg (WebAssembly) using the embedded fonts (preview-assets.js, generated from
// scripts/preview-fonts/*.ttf and node_modules/@resvg/resvg-wasm/index_bg.wasm).
// A card only ever holds: the client's logo and name, a summary (count, shapes, carat range),
// one of OUR re-hosted stone images, and Pure Carbon Group branding. Never prices, report
// numbers or anything from a vendor.
const { PNG } = require('pngjs');
const jpeg = require('jpeg-js');
const { initWasm, Resvg } = require('@resvg/resvg-wasm');
const assets = require('./preview-assets');
const { decode, makeIcon } = require('./icons');

const W = 1200, H = 630;
const GOLD = '#c9a84c', BG = '#0c0c0c';
const IMG_W = 560;                                   // width of the stone photo panel

let ready = null;
function init() {
  if (!ready) ready = initWasm(Buffer.from(assets.wasm, 'base64'));
  return ready;
}
const FONTS = () => [assets.cormorant, assets.inter, assets.interBold].map((b) => Buffer.from(b, 'base64'));

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const titleCase = (s) => String(s).trim().toLowerCase().replace(/\b[a-z]/g, (c) => c.toUpperCase());

// { n, shapes, min, max, text } from a quote's stones (cert_data.shape / cert_data.carat only).
function summarize(stones) {
  stones = Array.isArray(stones) ? stones : [];
  const n = stones.length;
  const shapes = [...new Set(stones.map((s) => titleCase((s && s.cert_data && s.cert_data.shape) || '')).filter(Boolean))];
  const cts = stones.map((s) => parseFloat(s && s.cert_data && s.cert_data.carat)).filter((c) => isFinite(c) && c > 0);
  const min = cts.length ? Math.min(...cts) : null, max = cts.length ? Math.max(...cts) : null;
  const parts = [];
  if (n) {
    const noun = `diamond${n === 1 ? '' : 's'}`;
    if (shapes.length === 1) parts.push(`${n} ${shapes[0].toLowerCase()} ${noun}`);
    else if (shapes.length >= 2 && shapes.length <= 3) parts.push(`${n} ${noun}`, shapes.map((s) => s.toLowerCase()).join(', '));
    else if (shapes.length > 3) parts.push(`${n} ${noun}`, 'mixed shapes');
    else parts.push(`${n} ${noun}`);
    if (min != null) parts.push(min === max ? `${min.toFixed(2)} ct` : `${min.toFixed(2)}–${max.toFixed(2)} ct`);
  }
  return { n, shapes, min, max, text: parts.join(' · ') };
}

// Square, padded logo (same treatment as the favicons) as a PNG data URI; null if unusable.
function logoUri(dataUri) {
  try { return 'data:image/png;base64,' + makeIcon(dataUri, 320).toString('base64'); } catch (e) { return null; }
}

// Stone photo, cover-cropped to w x h (area-averaged) and re-encoded as a JPEG data URI; null if unusable.
function photoUri(buf, w, h) {
  try {
    const img = decode('data:image/' + (buf[0] === 0xff ? 'jpeg' : 'png') + ';base64,' + Buffer.from(buf).toString('base64'));
    const s = Math.max(w / img.w, h / img.h);
    const cw = w / s, ch = h / s, cx = (img.w - cw) / 2, cy = (img.h - ch) / 2;
    const out = Buffer.alloc(w * h * 4);
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const x0 = cx + x / s, x1 = cx + (x + 1) / s, y0 = cy + y / s, y1 = cy + (y + 1) / s;
      let r = 0, g = 0, b = 0, a = 0, area = 0;
      for (let sy = Math.floor(y0); sy < Math.min(img.h, Math.ceil(y1)); sy++) {
        const wy = Math.min(sy + 1, y1) - Math.max(sy, y0);
        for (let sx = Math.floor(x0); sx < Math.min(img.w, Math.ceil(x1)); sx++) {
          const wt = wy * (Math.min(sx + 1, x1) - Math.max(sx, x0)), i = (sy * img.w + sx) * 4, al = img.d[i + 3] / 255;
          r += wt * al * img.d[i]; g += wt * al * img.d[i + 1]; b += wt * al * img.d[i + 2]; a += wt * al; area += wt;
        }
      }
      const o = (y * w + x) * 4;                      // transparent areas become the card's background
      const al = area ? a / area : 0, k = a ? 1 / a : 0;
      out[o] = Math.round(r * k * al + 12 * (1 - al)); out[o + 1] = Math.round(g * k * al + 12 * (1 - al));
      out[o + 2] = Math.round(b * k * al + 12 * (1 - al)); out[o + 3] = 255;
    }
    return 'data:image/jpeg;base64,' + jpeg.encode({ data: out, width: w, height: h }, 88).data.toString('base64');
  } catch (e) { return null; }
}

// Rough text width (px) for fitting: Cormorant ~0.44em per character, Inter ~0.56em.
const fit = (text, avail, max, min, em) => Math.max(min, Math.min(max, Math.floor(avail / (Math.max(1, text.length) * em))));
function clip(text, avail, size, em) {
  const maxChars = Math.floor(avail / (size * em));
  return text.length > maxChars ? text.slice(0, Math.max(1, maxChars - 1)).trimEnd() + '…' : text;
}

// opts: { name, summary, logo (data URI), pcgLogo (data URI), photo (data URI | null), neutral }
function cardSvg(o) {
  const defs = `<defs>
    <radialGradient id="glow" cx="0.2" cy="0.1" r="0.9"><stop offset="0" stop-color="${GOLD}" stop-opacity="0.13"/><stop offset="1" stop-color="${GOLD}" stop-opacity="0"/></radialGradient>
    <linearGradient id="fade" x1="0" x2="1" y1="0" y2="0"><stop offset="0" stop-color="${BG}" stop-opacity="1"/><stop offset="1" stop-color="${BG}" stop-opacity="0"/></linearGradient>
    <linearGradient id="shade" x1="0" x2="0" y1="0" y2="1"><stop offset="0.55" stop-color="${BG}" stop-opacity="0"/><stop offset="1" stop-color="${BG}" stop-opacity="0.55"/></linearGradient>
  </defs>`;
  const base = `<rect width="${W}" height="${H}" fill="${BG}"/><rect width="${W}" height="${H}" fill="url(#glow)"/>
    <rect x="24" y="24" width="${W - 48}" height="${H - 48}" fill="none" stroke="${GOLD}" stroke-opacity="0.22" stroke-width="1.5"/>`;
  let body;
  if (o.neutral) {                                    // expired / unknown: the Pure Carbon logo only
    body = `<clipPath id="lg"><rect x="${W / 2 - 110}" y="${H / 2 - 110}" width="220" height="220" rx="46"/></clipPath>
      <rect x="${W / 2 - 112}" y="${H / 2 - 112}" width="224" height="224" rx="48" fill="none" stroke="${GOLD}" stroke-opacity="0.5" stroke-width="2"/>
      ${o.pcgLogo ? `<image x="${W / 2 - 110}" y="${H / 2 - 110}" width="220" height="220" href="${o.pcgLogo}" clip-path="url(#lg)"/>` : ''}`;
    return svg(defs + base + body);
  }
  const hasPhoto = !!o.photo;
  const colW = hasPhoto ? W - IMG_W - 72 - 40 : W - 200;      // usable text width
  const cx = hasPhoto ? 72 : W / 2, anchor = hasPhoto ? 'start' : 'middle';
  const name = String(o.name || '');
  // Name: one line, or two when it is long (split at the space nearest the middle)
  let nameLines = [name], nameSize = fit(name, colW, hasPhoto ? 68 : 84, 34, 0.46);
  if (name.length * 52 * 0.46 > colW && name.includes(' ')) {
    const mid = name.length / 2, spaces = [...name].map((c, i) => (c === ' ' ? i : -1)).filter((i) => i >= 0);
    const cut = spaces.reduce((a, b) => (Math.abs(b - mid) < Math.abs(a - mid) ? b : a));
    nameLines = [name.slice(0, cut), name.slice(cut + 1)];
    nameSize = fit(nameLines.reduce((a, b) => (b.length > a.length ? b : a)), colW, hasPhoto ? 56 : 72, 30, 0.46);
  }
  nameLines = nameLines.map((l) => clip(l, colW, nameSize, 0.46));
  const logoSize = hasPhoto ? 116 : 148;
  const logoX = hasPhoto ? cx : cx - logoSize / 2, logoY = hasPhoto ? 96 : 92;
  const nameY = logoY + logoSize + (hasPhoto ? 84 : 92);
  const ruleY = nameY + (nameLines.length - 1) * Math.round(nameSize * 1.05) + 30;
  // summary: one line if it fits, else split at the separators
  const sum = String(o.summary || '');
  let lines = sum ? [sum] : [];
  const sumSize = hasPhoto ? 27 : 32;
  if (sum && sum.length * sumSize * 0.56 > colW) lines = sum.split(' · ').reduce((acc, p) => {
    const last = acc[acc.length - 1];
    if (last !== undefined && (last + ' · ' + p).length * sumSize * 0.56 <= colW) acc[acc.length - 1] = last + ' · ' + p; else acc.push(p);
    return acc;
  }, []);
  const sumSvg = lines.map((l, i) => `<text x="${cx}" y="${ruleY + 58 + i * (sumSize + 14)}" font-family="Inter" font-weight="${i === 0 ? 600 : 400}" font-size="${sumSize}" fill="${i === 0 ? '#ecebe4' : GOLD}" text-anchor="${anchor}">${esc(clip(l, colW, sumSize, 0.58))}</text>`).join('');
  const logoSvg = o.logo
    ? `<clipPath id="lg"><rect x="${logoX}" y="${logoY}" width="${logoSize}" height="${logoSize}" rx="${Math.round(logoSize * 0.21)}"/></clipPath>
       <rect x="${logoX - 2}" y="${logoY - 2}" width="${logoSize + 4}" height="${logoSize + 4}" rx="${Math.round(logoSize * 0.21) + 2}" fill="none" stroke="${GOLD}" stroke-opacity="0.55" stroke-width="1.5"/>
       <image x="${logoX}" y="${logoY}" width="${logoSize}" height="${logoSize}" href="${o.logo}" clip-path="url(#lg)"/>` : '';
  const photoSvg = hasPhoto
    ? `<clipPath id="ph"><rect x="${W - IMG_W}" y="0" width="${IMG_W}" height="${H}"/></clipPath>
       <image x="${W - IMG_W}" y="0" width="${IMG_W}" height="${H}" href="${o.photo}" clip-path="url(#ph)"/>
       <rect x="${W - IMG_W}" y="0" width="${IMG_W}" height="${H}" fill="url(#shade)"/>
       <rect x="${W - IMG_W}" y="0" width="150" height="${H}" fill="url(#fade)"/>
       <rect x="${W - IMG_W}" y="0" width="${IMG_W}" height="${H}" fill="none" stroke="${GOLD}" stroke-opacity="0.22" stroke-width="1.5" clip-path="url(#ph)"/>` : '';
  const brandY = H - 62;
  const brand = `<clipPath id="r6"><rect x="${hasPhoto ? 72 : W / 2 - 140}" y="${brandY - 21}" width="28" height="28" rx="6"/></clipPath>` +
    (hasPhoto
      ? `${o.pcgLogo ? `<image x="72" y="${brandY - 21}" width="28" height="28" href="${o.pcgLogo}" clip-path="url(#r6)" opacity="0.85"/>` : ''}
         <text x="${o.pcgLogo ? 112 : 72}" y="${brandY}" font-family="Inter" font-weight="600" font-size="14" letter-spacing="4.5" fill="${GOLD}" fill-opacity="0.7">PURE CARBON GROUP</text>`
      : `${o.pcgLogo ? `<image x="${W / 2 - 140}" y="${brandY - 21}" width="28" height="28" href="${o.pcgLogo}" clip-path="url(#r6)" opacity="0.85"/>` : ''}
         <text x="${W / 2 - 100}" y="${brandY}" font-family="Inter" font-weight="600" font-size="14" letter-spacing="4.5" fill="${GOLD}" fill-opacity="0.7">PURE CARBON GROUP</text>`);
  body = `${photoSvg}${logoSvg}
    ${nameLines.map((l, i) => `<text x="${cx}" y="${nameY + i * Math.round(nameSize * 1.05)}" font-family="Cormorant Garamond" font-weight="500" font-size="${nameSize}" fill="#f4f2ea" text-anchor="${anchor}">${esc(l)}</text>`).join('')}
    <rect x="${hasPhoto ? cx : cx - 28}" y="${ruleY}" width="56" height="2" fill="${GOLD}"/>
    ${sumSvg}${brand}`;
  return svg(defs + base + body);
}
const svg = (inner) => `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">${inner}</svg>`;

// -> JPEG Buffer (1200x630). WhatsApp only shows preview images under ~300 KB; these are ~60-150 KB.
async function renderCard(o) {
  await init();
  const s = cardSvg(o);
  const png = new Resvg(s, { fitTo: { mode: 'original' }, font: { fontBuffers: FONTS(), loadSystemFonts: false, defaultFontFamily: 'Inter' } }).render().asPng();
  const raw = PNG.sync.read(Buffer.from(png));
  return Buffer.from(jpeg.encode({ data: raw.data, width: raw.width, height: raw.height }, 86).data);
}

module.exports = { W, H, IMG_W, summarize, logoUri, photoUri, cardSvg, renderCard };
