// Square favicon / home-screen icons made from a client's logo (a data URI: PNG or JPEG).
// The logo is scaled to fit (never stretched) and centred on a square background: the logo's own
// edge colour when it has a solid edge, else white or near-black, whichever contrasts with it.
// Pure JavaScript (pngjs + jpeg-js), so it runs anywhere the functions do.
const { PNG } = require('pngjs');
const jpeg = require('jpeg-js');

const SIZES = [32, 180, 512];
const PAD = 0.06;                                   // margin around the logo, as a share of the icon

function decode(dataUri) {
  const m = /^data:(image\/[a-z+]+);base64,(.+)$/.exec(String(dataUri || ''));
  if (!m) throw new Error('not a data URI');
  const buf = Buffer.from(m[2], 'base64');
  if (buf[0] === 0x89 && buf.slice(1, 4).toString() === 'PNG') { const p = PNG.sync.read(buf); return { w: p.width, h: p.height, d: p.data }; }
  if (buf[0] === 0xff && buf[1] === 0xd8) { const j = jpeg.decode(buf, { useTArray: true, formatAsRGBA: true }); return { w: j.width, h: j.height, d: j.data }; }
  throw new Error('unsupported image type (only PNG and JPEG can be used)');
}

// Solid edge colour, or null when the logo's edge is transparent / mixed.
function edgeColour(img) {
  let n = 0, r = 0, g = 0, b = 0, solid = 0;
  const at = (x, y) => (y * img.w + x) * 4;
  const edge = [];
  for (let x = 0; x < img.w; x++) { edge.push(at(x, 0), at(x, img.h - 1)); }
  for (let y = 0; y < img.h; y++) { edge.push(at(0, y), at(img.w - 1, y)); }
  for (const i of edge) {
    n++;
    if (img.d[i + 3] > 240) { solid++; r += img.d[i]; g += img.d[i + 1]; b += img.d[i + 2]; }
  }
  if (!solid || solid / n < 0.9) return null;
  return [Math.round(r / solid), Math.round(g / solid), Math.round(b / solid)];
}

function contrastBg(img) {                          // white, or near-black when the visible logo is light
  let lum = 0, wt = 0;
  for (let i = 0; i < img.d.length; i += 4) {
    const a = img.d[i + 3] / 255;
    lum += a * (0.299 * img.d[i] + 0.587 * img.d[i + 1] + 0.114 * img.d[i + 2]);
    wt += a;
  }
  return wt && lum / wt > 165 ? [24, 24, 24] : [255, 255, 255];
}

// Area-average scaling of the logo onto a size x size square, premultiplied alpha, over bg.
function makeIcon(dataUri, size) {
  const img = decode(dataUri);
  if (!(img.w > 0 && img.h > 0)) throw new Error('empty image');
  const bg = edgeColour(img) || contrastBg(img);
  const box = size * (1 - 2 * PAD);
  const scale = Math.min(box / img.w, box / img.h);
  const dw = Math.max(1, Math.round(img.w * scale)), dh = Math.max(1, Math.round(img.h * scale));
  const ox = Math.floor((size - dw) / 2), oy = Math.floor((size - dh) / 2);
  const out = new PNG({ width: size, height: size });
  for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
    let r = bg[0], g = bg[1], b = bg[2];
    if (x >= ox && x < ox + dw && y >= oy && y < oy + dh) {
      const sx0 = (x - ox) * img.w / dw, sx1 = (x - ox + 1) * img.w / dw;
      const sy0 = (y - oy) * img.h / dh, sy1 = (y - oy + 1) * img.h / dh;
      let pr = 0, pg = 0, pb = 0, pa = 0, area = 0;
      for (let sy = Math.floor(sy0); sy < Math.min(img.h, Math.ceil(sy1)); sy++) {
        const wy = Math.min(sy + 1, sy1) - Math.max(sy, sy0);
        for (let sx = Math.floor(sx0); sx < Math.min(img.w, Math.ceil(sx1)); sx++) {
          const w = wy * (Math.min(sx + 1, sx1) - Math.max(sx, sx0));
          const i = (sy * img.w + sx) * 4, a = img.d[i + 3] / 255;
          pr += w * a * img.d[i]; pg += w * a * img.d[i + 1]; pb += w * a * img.d[i + 2]; pa += w * a; area += w;
        }
      }
      if (area > 0) {
        const a = pa / area;
        r = pa ? (pr / pa) * a + bg[0] * (1 - a) : bg[0];
        g = pa ? (pg / pa) * a + bg[1] * (1 - a) : bg[1];
        b = pa ? (pb / pa) * a + bg[2] * (1 - a) : bg[2];
      }
    }
    const o = (y * size + x) * 4;
    out.data[o] = Math.round(r); out.data[o + 1] = Math.round(g); out.data[o + 2] = Math.round(b); out.data[o + 3] = 255;
  }
  return PNG.sync.write(out);
}

// <head> tags for a client slug: 32px and 512px icons, 180px apple-touch-icon (same-domain URLs).
function iconTags(slug) {
  const s = encodeURIComponent(String(slug || 'pure-carbon-group'));
  return `<link rel="icon" type="image/png" sizes="32x32" href="/client-logo/${s}/icon-32.png">
  <link rel="icon" type="image/png" sizes="512x512" href="/client-logo/${s}/icon-512.png">
  <link rel="apple-touch-icon" sizes="180x180" href="/client-logo/${s}/icon-180.png">`;
}

module.exports = { SIZES, decode, makeIcon, iconTags };
