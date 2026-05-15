const { createClient } = require('@supabase/supabase-js');

const supabase = createClient(
  process.env.SUPABASE_URL,
  process.env.SUPABASE_ANON_KEY
);

function generateId(length = 8) {
  const chars = 'abcdefghijkmnpqrstuvwxyz23456789';
  let id = '';
  for (let i = 0; i < length; i++) {
    id += chars[Math.floor(Math.random() * chars.length)];
  }
  return id;
}

exports.handler = async function(event) {
  if (event.httpMethod !== 'POST') {
    return { statusCode: 405, body: 'Method Not Allowed' };
  }

  try {
    const { quote_id, client, stones, retail_prices } = JSON.parse(event.body);

    const id = generateId();
    const expires_at = new Date(Date.now() + 30 * 24 * 60 * 60 * 1000).toISOString();

    const { error } = await supabase
      .from('shares')
      .insert({ id, quote_id, client, stones, retail_prices: retail_prices || null, expires_at });

    if (error) {
      return { statusCode: 500, body: JSON.stringify({ error: error.message }) };
    }

    const longUrl = `${process.env.QUOTE_BASE_URL}/s/${id}`;

    let shortLink = longUrl;
    try {
      const bitlyRes = await fetch('https://api-ssl.bitly.com/v4/shorten', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${process.env.BITLY_TOKEN}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ long_url: longUrl, domain: 'purecarbondiamonds.tv' })
      });
      const bitlyData = await bitlyRes.json();
      if (bitlyData.link) shortLink = bitlyData.link;
    } catch(e) {}

    return {
      statusCode: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, link: shortLink })
    };

  } catch(err) {
    return { statusCode: 500, body: JSON.stringify({ error: err.message }) };
  }
};
