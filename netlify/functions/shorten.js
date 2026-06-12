exports.handler = async (event) => {
  if (event.httpMethod !== 'POST') {
    return { statusCode: 405, body: 'Method Not Allowed' };
  }

  const { long_url } = JSON.parse(event.body || '{}');
  console.log('STEP 1: received long_url:', long_url ? 'yes' : 'MISSING');
  if (!long_url) {
    return { statusCode: 400, body: JSON.stringify({ error: 'Missing long_url' }) };
  }

  const SUPABASE_URL = process.env.SUPABASE_URL;
  const SUPABASE_KEY = process.env.SUPABASE_SERVICE_KEY;
  const BITLY_TOKEN = process.env.BITLY_TOKEN;
  console.log('STEP 2: env check — SUPABASE_URL:', !!SUPABASE_URL, '| SUPABASE_KEY:', !!SUPABASE_KEY, '| BITLY_TOKEN:', !!BITLY_TOKEN);

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

  console.log('STEP 3: supabase insert status:', res.status);
  if (!res.ok) {
    const errText = await res.text();
    console.log('STEP 3 ERROR:', errText);
    return { statusCode: 500, body: JSON.stringify({ error: 'Failed to save link' }) };
  }

  const rLink = `https://alldiamondeverything.com/r/${code}`;

  try {
    const bitlyRes = await fetch('https://api-ssl.bitly.com/v4/shorten', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${BITLY_TOKEN}`
      },
      body: JSON.stringify({
        long_url: rLink,
        domain: 'purecarbondiamonds.tv'
      })
    });

    console.log('STEP 4: bitly status:', bitlyRes.status);
    const bitlyData = await bitlyRes.json();
    console.log('STEP 4: bitly response:', JSON.stringify(bitlyData));

    if (bitlyRes.ok && bitlyData.link) {
      return {
        statusCode: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ link: bitlyData.link })
      };
    }
  } catch (e) {
    console.log('STEP 4 EXCEPTION:', e.message);
  }

  console.log('STEP 5: falling back to /r/ link');
  return {
    statusCode: 200,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ link: rLink })
  };
};
