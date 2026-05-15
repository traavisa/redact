exports.handler = async function(event) {
  if (event.httpMethod !== 'POST') {
    return { statusCode: 405, body: 'Method Not Allowed' };
  }

  try {
    const { password } = JSON.parse(event.body);
    const correct = process.env.QUOTE_PASSWORD || 'PCD';

    if (password && password.toUpperCase().trim() === correct.toUpperCase().trim()) {
      return {
        statusCode: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ok: true })
      };
    }

    return {
      statusCode: 401,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ok: false })
    };

  } catch(err) {
    return { statusCode: 500, body: JSON.stringify({ error: 'Internal error' }) };
  }
};
