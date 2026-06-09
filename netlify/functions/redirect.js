exports.handler = async (event) => {
  const code = event.path.replace('/r/', '').replace(/\//g, '');
  if (!code) {
    return { statusCode: 400, body: 'Missing code' };
  }

  const SUPABASE_URL = process.env.SUPABASE_URL;
  const SUPABASE_KEY = process.env.SUPABASE_SERVICE_KEY;

  const res = await fetch(
    `${SUPABASE_URL}/rest/v1/redirects?code=eq.${code}&select=url&limit=1`,
    {
      headers: {
        'apikey': SUPABASE_KEY,
        'Authorization': `Bearer ${SUPABASE_KEY}`
      }
    }
  );

  const data = await res.json();
  if (!data || !data[0]) {
    return { statusCode: 404, body: 'Link not found' };
  }

  return {
    statusCode: 302,
    headers: { Location: data[0].url }
  };
};
