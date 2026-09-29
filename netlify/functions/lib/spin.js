// Which 360 captures may be shown. Same rule as the app (spin_jobs.trusted_spin):
// at least 24 frames AND made by the current capture code (version >= TRUSTED_VERSION: the
// certificate's 360 API fields only; v4 = JPEG frames, v5+ = WebP). Older captures are never
// served: the stone falls back to its original viewer.
const CAPTURE_VERSION = 6;     // keep equal to spin_capture.CAPTURE_VERSION (v5 = WebP frames, v6 = plus NNN@hi.webp full-quality frames)
const TRUSTED_VERSION = 4;     // keep equal to spin_capture.TRUSTED_VERSION
const MIN_FRAMES = 24;
const ID_RE = /^[a-f0-9]{32}$/;
// KILL SWITCH: the Netlify environment variable SPIN_ENABLED.
//   unset / anything else = OFF: every page, /v/ link and function ignores all captures and
//     shows the stone's original viewer instead.
//   "test" = captured frames are shown ONLY on quotes for TEST_CLIENT (get-quote), and on
//     share links made from them; /v/ links always show the original viewer.
//   "true" = captured frames are shown everywhere.
const SPIN_MODE = (() => {
  const v = String(process.env.SPIN_ENABLED || '').trim().toLowerCase();
  return v === 'true' ? 'on' : v === 'test' ? 'test' : 'off';
})();
const SPIN_ENABLED = SPIN_MODE !== 'off';
const TEST_CLIENT = 'Pure Carbon Group';
// Whether captures may be shown on a quote for this client
function spinsForClient(client) {
  if (SPIN_MODE === 'on') return true;
  return SPIN_MODE === 'test' && String(client || '').trim().toLowerCase() === TEST_CLIENT.toLowerCase();
}

function cleanSpin(id, n, top, v) {
  id = String(id || ''); n = Number(n); top = Number(top); v = Number(v);
  if (!ID_RE.test(id) || !Number.isInteger(n) || n < MIN_FRAMES || n > 720) return null;
  return { id, n, top: Number.isInteger(top) && top >= 0 && top < n ? top : 0, v };
}
const currentCode = (v) => Number.isInteger(Number(v)) && Number(v) >= TRUSTED_VERSION;
// A spin saved on a quote stone: { id, n, top, v }
function trustedSpin(sp) {
  if (!SPIN_ENABLED || !sp || typeof sp !== 'object') return null;
  return currentCode(sp.v) ? cleanSpin(sp.id, sp.n, sp.top, sp.v) : null;
}
// A media_links row: spin_id, spin_frames, spin_top, spin_version
function trustedRow(row) {
  if (!SPIN_ENABLED || !row || !row.spin_id) return null;
  return currentCode(row.spin_version) ? cleanSpin(row.spin_id, row.spin_frames, row.spin_top, row.spin_version) : null;
}
// Reads media_links with the newest columns first (older databases lack some of them)
async function mediaLinks(filter, extra = '') {
  const KEY = process.env.SUPABASE_SERVICE_KEY;
  for (const cols of ['token,vendor_url,spin_id,spin_frames,spin_top,spin_version', 'token,vendor_url,spin_id,spin_frames,spin_top',
                      'token,vendor_url,spin_id,spin_frames', 'token,vendor_url']) {
    const res = await fetch(`${process.env.SUPABASE_URL}/rest/v1/media_links?${filter}&select=${cols}${extra}`,
      { headers: { apikey: KEY, Authorization: `Bearer ${KEY}` } });
    if (res.ok) return await res.json();
    if (res.status !== 400) return null;
  }
  return null;
}
// The media_links row a capture belongs (or belonged, before a re-capture) to
async function rowForSpin(id) {
  if (!ID_RE.test(String(id || ''))) return null;
  let rows = await mediaLinks(`or=(spin_id.eq.${id},old_spin_ids.cs.%7B${id}%7D)`, '&limit=1').catch(() => null);
  if (!rows) rows = await mediaLinks(`spin_id=eq.${id}`, '&limit=1').catch(() => null);
  return rows && rows[0] ? rows[0] : null;
}
module.exports = { SPIN_ENABLED, SPIN_MODE, TEST_CLIENT, spinsForClient, CAPTURE_VERSION, TRUSTED_VERSION, MIN_FRAMES, trustedSpin, trustedRow, mediaLinks, rowForSpin };
