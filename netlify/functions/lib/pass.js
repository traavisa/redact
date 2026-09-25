// Short-lived signed pass for the password-protected landing page.
// Issued by verify-password, checked by create-media-link. Signed with a server-only secret.
const crypto = require('crypto');

const HOURS = 12;
const secret = () => process.env.LINK_SIGNING_SECRET || process.env.SUPABASE_SERVICE_KEY || '';

function sign(exp) {
  return crypto.createHmac('sha256', secret()).update('ade-pass:' + exp).digest('base64url');
}

exports.issuePass = function () {
  const exp = Math.floor(Date.now() / 1000) + HOURS * 3600;
  return `${exp}.${sign(exp)}`;
};

exports.validPass = function (pass) {
  if (!secret() || typeof pass !== 'string') return false;
  const [exp, sig] = pass.split('.');
  if (!/^\d{10}$/.test(exp || '') || !sig) return false;
  if (Number(exp) < Date.now() / 1000) return false;
  const want = Buffer.from(sign(exp));
  const got = Buffer.from(sig);
  return want.length === got.length && crypto.timingSafeEqual(want, got);
};
