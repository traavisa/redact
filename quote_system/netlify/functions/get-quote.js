const { createClient } = require('@supabase/supabase-js');

const supabase = createClient(
  process.env.SUPABASE_URL,
  process.env.SUPABASE_ANON_KEY
);

exports.handler = async function(event) {
  const id = event.path.split('/').pop();
  if (!id) return { statusCode: 400, body: 'Missing ID' };

  const { data, error } = await supabase
    .from('quotes')
    .select('*')
    .eq('id', id)
    .single();

  if (error || !data) {
    return { statusCode: 404, body: JSON.stringify({ error: 'Quote not found' }) };
  }

  if (new Date(data.expires_at) < new Date()) {
    return { statusCode: 410, body: JSON.stringify({ error: 'Quote expired' }) };
  }

  return {
    statusCode: 200,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data)
  };
};
