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
    const { client, stones, expiry_days = 30 } = JSON.parse(event.body);

    if (!client || !stones || !Array.isArray(stones) || stones.length === 0) {
      return { statusCode: 400, body: JSON.stringify({ error: 'Missing required fields' }) };
    }

    // Upload PDFs to Supabase Storage
    const storedStones = [];
    for (const stone of stones) {
      let pdf_url = null;

      if (stone.pdf_base64 && stone.pdf_name) {
        const pdfBuffer = Buffer.from(stone.pdf_base64, 'base64');
        const filename = `${Date.now()}_${stone.pdf_name}`;

        const { data, error } = await supabase.storage
          .from('certificates')
          .upload(filename, pdfBuffer, {
            contentType: 'application/pdf',
            upsert: false
          });

        if (!error) {
          const { data: urlData } = supabase.storage
            .from('certificates')
            .getPublicUrl(filename);
          pdf_url = urlData.publicUrl;
        }
      }

      storedStones.push({
        cert_last4:   stone.cert_last4,
        video_url:    stone.video_url,
        pdf_url,
        price:        stone.price,
        currency:     stone.currency,
        price_type:   stone.price_type,
        carat_weight: stone.carat_weight,
      });
    }

    const id = generateId();
    const expires_at = new Date(Date.now() + expiry_days * 24 * 60 * 60 * 1000).toISOString();

    const { error: dbError } = await supabase
      .from('quotes')
      .insert({ id, client, stones: storedStones, expires_at });

    if (dbError) {
      return { statusCode: 500, body: JSON.stringify({ error: dbError.message }) };
    }

    // Build long URL and shorten via Bitly
    const longUrl = `${process.env.QUOTE_BASE_URL}/q/${id}`;

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
