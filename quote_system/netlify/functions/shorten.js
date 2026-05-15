exports.handler = async function(event) {
  if (event.httpMethod !== 'POST') {
    return { statusCode: 405, body: 'Method Not Allowed' };
  }
  try {
    const { long_url } = JSON.parse(event.body);
    if (!long_url || (!long_url.startsWith('http://') && !long_url.startsWith('https://'))) {
      return { statusCode: 400, body: JSON.stringify({ error: 'Invalid URL' }) };
    }
    const response = await fetch('https://api-ssl.bitly.com/v4/shorten', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${process.env.BITLY_TOKEN}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ long_url, domain: 'purecarbondiamonds.tv' })
    });
    const data = await response.json();
    return {
      statusCode: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ link: data.link || null })
    };
  } catch (err) {
    return { statusCode: 500, body: JSON.stringify({ error: 'Internal error' }) };
  }
};
