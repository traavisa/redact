const { createClient } = require('@supabase/supabase-js');
const fs = require('fs');
const path = require('path');

const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_ANON_KEY);

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function shapesLabel(stones) {
  const shapes = (stones || []).map((s) => (s.cert_data && s.cert_data.shape) || '').filter(Boolean);
  if (!shapes.length) return '';
  const unique = [...new Set(shapes)];
  if (unique.length === 1) {
    const shape = unique[0];
    return shapes.length > 1 && !shape.toLowerCase().endsWith('s') ? `${shape}s` : shape;
  }
  return unique.join(', ');
}

exports.handler = async function (event) {
  const id = event.path.split('/').filter(Boolean).pop();
  const html = fs.readFileSync(path.join(__dirname, '..', '..', 'share.html'), 'utf8');

  let title = 'Diamond Options';
  let description = 'View your diamond selection.';

  if (id) {
    try {
      const { data } = await supabase.from('shares').select('*').eq('id', id).single();
      if (data) {
        const client = data.client || '';
        const shapes = shapesLabel(data.stones);
        title = client ? `${client} Diamond Options` : 'Diamond Options';
        if (shapes) title += ` — ${shapes}`;
        const n = (data.stones || []).length;
        description = shapes
          ? `${n} diamond${n === 1 ? '' : 's'} selected — ${shapes}`
          : `${n} diamond${n === 1 ? '' : 's'} selected`;
      }
    } catch (e) {
      // fall back to defaults on any lookup failure
    }
  }

  const metaBlock = `<meta property="og:title" content="${escapeHtml(title)}" />
  <meta property="og:description" content="${escapeHtml(description)}" />
  <meta property="og:type" content="website" />
  <meta name="twitter:card" content="summary" />
  <meta name="twitter:title" content="${escapeHtml(title)}" />
  <meta name="twitter:description" content="${escapeHtml(description)}" />`;

  const out = html.replace(
    '<title>Diamond Options</title>',
    `<title>${escapeHtml(title)}</title>\n  ${metaBlock}`
  );

  return {
    statusCode: 200,
    headers: { 'Content-Type': 'text/html; charset=utf-8' },
    body: out,
  };
};
