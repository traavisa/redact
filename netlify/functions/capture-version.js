// The current 360 capture code version (deployed from main with this site). The app on
// Render refuses to capture or run the 360 upgrade when its own version is older.
const { CAPTURE_VERSION, TRUSTED_VERSION } = require('./lib/spin');

exports.handler = async function () {
  return {
    statusCode: 200,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
    body: JSON.stringify({ version: CAPTURE_VERSION, trusted: TRUSTED_VERSION }),
  };
};
