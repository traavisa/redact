// Old customer share links (/s/<id>) are retired.
// This page loads NO share data and never contacts the database.
const PAGE = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="robots" content="noindex">
  <meta name="referrer" content="no-referrer">
  <title>Link no longer active</title>
  <style>
    html, body { margin: 0; min-height: 100%; background: #0c0c0c; color: #e8e8e0;
      font-family: -apple-system, 'Inter', 'Segoe UI', sans-serif; }
    .wrap { min-height: 100vh; display: flex; align-items: center; justify-content: center;
      padding: 2rem; box-sizing: border-box; text-align: center; }
    .box { max-width: 340px; }
    .dot { width: 6px; height: 6px; border-radius: 50%; background: #c9a84c; margin: 0 auto 18px; }
    h1 { font-size: 18px; font-weight: 400; color: #ddd; margin: 0 0 10px; }
    p { font-size: 13px; color: #8a8a8a; line-height: 1.7; margin: 0; }
  </style>
</head>
<body>
  <div class="wrap"><div class="box">
    <div class="dot"></div>
    <h1>This link is no longer active</h1>
    <p>Please ask your jeweller for a new link.</p>
  </div></div>
</body>
</html>`;

exports.handler = async function () {
  return {
    statusCode: 410,
    headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'public, max-age=3600' },
    body: PAGE,
  };
};
