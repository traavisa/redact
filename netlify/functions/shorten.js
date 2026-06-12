exports.handler = async (event) => {
  if (event.httpMethod !== 'POST') {
    return { statusCode: 405, body: 'Method Not Allowed' };
  }

  const { long_url } = JSON.parse(event.body || '{}');
  if (!long_url) {
    return { statusCode: 400, body: JSON.stringify({ error: 'Missing long_url' }) };
  }

  const SUPABASE_URL = process.env.SUPABASE_URL;
  const SUPABASE_KEY = process.env.SUPABASE_SERVICE_KEY;

  const code = Math.random().toString(36).slice(2, 8);

  const res = await fetch(`${SUPABASE_URL}/rest/v1/redirects`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'apikey': SUPABASE_KEY,
      'Authorization': `Bearer ${SUPABASE_KEY}`,
      'Prefer': 'return=minimal'
    },
    body: JSON.stringify({ code, url: long_url })
  });

  if (!res.ok) {
    return { statusCode: 500, body: JSON.stringify({ error: 'Failed to save link' }) };
  }

  return {
    statusCode: 200,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ link: `https://alldiamondeverything.com/r/${code}` })
  };
};
