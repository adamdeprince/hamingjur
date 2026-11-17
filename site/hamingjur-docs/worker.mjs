/** Serve the public documentation from its dedicated R2 bucket. */
export default {
  async fetch(request, env) {
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return new Response('Method not allowed', { status: 405, headers: { Allow: 'GET, HEAD' } });
    }
    const url = new URL(request.url);
    if (url.pathname === '/index.html') {
      return Response.redirect(env.SITE_URL + '/', 308);
    }
    let key;
    try {
      key = decodeURIComponent(url.pathname.slice(1)) || 'index.html';
    } catch {
      return new Response('Invalid URL', { status: 400 });
    }
    const object = request.method === 'HEAD' ? await env.DOCS.head(key) : await env.DOCS.get(key);
    if (object === null) {
      return new Response(request.method === 'HEAD' ? null : 'Not found', { status: 404 });
    }
    const headers = new Headers();
    object.writeHttpMetadata(headers);
    headers.set('ETag', object.httpEtag);
    headers.set('Cache-Control', 'public, max-age=300, must-revalidate');
    if (key.endsWith('.html')) headers.set('Content-Type', 'text/html; charset=utf-8');
    if (request.headers.get('If-None-Match') === object.httpEtag) {
      return new Response(null, { status: 304, headers });
    }
    return new Response(request.method === 'HEAD' ? null : object.body, { headers });
  }
};
