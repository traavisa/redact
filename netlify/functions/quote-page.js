const { createClient } = require('@supabase/supabase-js');
const fs = require('fs');
const path = require('path');
const { slugOf } = require('./lib/clients');
const { iconTags } = require('./lib/icons');
const { ogTags, origin } = require('./lib/og');

const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_SERVICE_KEY);

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function shapesLabel(stones) {
  const shapes = (stones || []).map((s) => (s.cert_data && s.cert_data.shape) || '').filter(Boolean);
  if (!shapes.length) return '';
  const unique = [...new Map(shapes.map((s) => [s.toLowerCase(), s.toLowerCase().replace(/\b[a-z]/g, (c) => c.toUpperCase())])).values()];   // same shape in any letter case counts once
  if (unique.length === 1) {
    const shape = unique[0];
    return shapes.length > 1 && !shape.toLowerCase().endsWith('s') ? `${shape}s` : shape;
  }
  return unique.join(', ');
}

exports.handler = async function (event) {
  const id = event.path.split('/').filter(Boolean).pop();
  const html = fs.readFileSync(path.join(__dirname, '..', '..', 'quote.html'), 'utf8');

  let title = 'Diamond Options';
  let description = 'View your diamond selection.';
  let slug = '';
  let expired = false;

  if (id) {
    try {
      const { data } = await supabase.from('quotes').select('client,stones,expires_at').eq('id', id).single();
      if (data) {
        const client = data.client || '';
        expired = new Date(data.expires_at) < new Date();
        slug = slugOf(client);
        const shapes = shapesLabel(data.stones);
        if (expired) {                                   // expired: neutral, nothing about the quote
          title = 'Diamond Options';
          description = 'This link has expired.';
        } else {
          title = client ? `${client} Diamond Options` : 'Diamond Options';
          if (shapes) title += ` \u2014 ${shapes}`;
          const n = (data.stones || []).length;
          description = shapes
            ? `${n} diamond${n === 1 ? '' : 's'} selected \u2014 ${shapes}`
            : `${n} diamond${n === 1 ? '' : 's'} selected`;
        }
      }
    } catch (e) {
      // fall back to defaults on any lookup failure
    }
  }

  // Server-side (crawlers don't run JavaScript): title, description and a 1200x630 preview image
  const base = origin(event);
  const metaBlock = ogTags({
    title, description, siteName: 'Pure Carbon Group',
    image: `${base}/og/${id && /^[a-z0-9]{4,32}$/i.test(id) ? 'q/' + id : 'neutral'}.jpg`,
    url: id ? `${base}/q/${encodeURIComponent(id)}` : '',
  });

  // Favicon + home-screen icon: this quote's client logo (square, from our domain); Pure Carbon's if none
  const out = html.replace(/<link rel="icon" id="favicon"[^>]*>/, iconTags(slug)).replace(
    '<title>Diamond Options</title>',
    `<title>${escapeHtml(title)}</title>\n  ${metaBlock}`
  );

  return {
    statusCode: 200,
    headers: { 'Content-Type': 'text/html; charset=utf-8' },
    body: out,
  };
};
