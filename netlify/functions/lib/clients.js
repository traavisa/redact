// Client logos. Order: (1) the built-in logos in quote.html's CLIENT_LOGOS list (a built-in client's
// logo can't be replaced by an in-app record with the same name), (2) clients added in the app
// ("Add a new client", table custom_clients, read with the server-side key, never sent as a table),
// (3) Pure Carbon Group. Logos are served from our own domain
// (/client-logo/<slug>) or inlined into the page; nothing comes from a vendor/supplier host.
const fs = require('fs');
const path = require('path');

const FALLBACK = 'Pure Carbon Group';
const DB_TTL_MS = 60 * 1000;                       // a client added in the app shows up within a minute
let BUILTIN = null;                                // slug -> { name, logo (data URI) }
let DB = { at: 0, map: {} };

function slugOf(name) {
  return String(name || '').toLowerCase().replace(/&/g, 'and').replace(/['’]/g, '')
    .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
}

function builtin() {
  if (BUILTIN) return BUILTIN;
  BUILTIN = {};
  try {
    const q = fs.readFileSync(path.join(__dirname, '..', '..', '..', 'quote.html'), 'utf8');
    const start = q.indexOf('const CLIENT_LOGOS = {');
    const block = q.slice(start, q.indexOf('\n};', start));
    const re = /^\s*"((?:[^"\\]|\\.)+)":\s*"(data:image\/[a-z+]+;base64,[A-Za-z0-9+/=]+)"/gm;
    let m;
    while ((m = re.exec(block))) BUILTIN[slugOf(m[1])] = { name: m[1], logo: m[2] };
  } catch (e) { /* no built-in logos: the fallbacks below still work */ }
  return BUILTIN;
}

function sniff(b64) {
  const head = Buffer.from(String(b64).slice(0, 24), 'base64');
  if (head[0] === 0xff && head[1] === 0xd8) return 'image/jpeg';
  if (head.slice(1, 4).toString() === 'PNG') return 'image/png';
  if (head.slice(0, 4).toString() === 'RIFF') return 'image/webp';
  return null;
}

async function custom() {
  if (Date.now() - DB.at < DB_TTL_MS) return DB.map;
  const url = process.env.SUPABASE_URL, key = process.env.SUPABASE_SERVICE_KEY;
  let map = DB.map;
  if (url && key) {
    try {
      const r = await fetch(`${url}/rest/v1/custom_clients?select=name,logo_b64&order=name.asc`,
        { headers: { apikey: key, Authorization: `Bearer ${key}` } });
      if (r.ok) {
        map = {};
        for (const row of await r.json()) {
          const type = row && row.name && row.logo_b64 ? sniff(row.logo_b64) : null;
          if (type) map[slugOf(row.name)] = { name: String(row.name), logo: `data:${type};base64,${row.logo_b64}` };
        }
      }
    } catch (e) { /* keep what we had */ }
  }
  DB = { at: Date.now(), map };
  return map;
}

// { name, logo } for a slug, or null when it isn't a known client (no fallback).
async function lookup(slug) {
  slug = String(slug || '');
  if (!slug) return null;
  return builtin()[slug] || (await custom())[slug] || null;
}

// Like lookup, but always has a logo: unknown clients get Pure Carbon Group's.
async function logoFor(slug) {
  return (await lookup(slug)) || builtin()[slugOf(FALLBACK)] || null;
}

function reset() { BUILTIN = null; DB = { at: 0, map: {} }; }

module.exports = { slugOf, lookup, logoFor, reset, FALLBACK };
