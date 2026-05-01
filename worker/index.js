const FPL_BASE = 'https://fantasy.premierleague.com/api/';

// Cache TTLs in seconds per endpoint pattern
function cacheTTL(path) {
  if (path.includes('bootstrap-static'))   return 3600;   // 1 hour
  if (path.includes('/live/'))             return 60;     // 1 minute
  if (path.includes('fixtures'))           return 3600;
  if (path.includes('element-summary'))    return 600;    // 10 minutes
  if (path.includes('/picks/'))            return 120;    // 2 minutes
  return 300;
}

const CORS_HEADERS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

export default {
  async fetch(request, env, ctx) {
    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: CORS_HEADERS });
    }

    const url = new URL(request.url);
    // Path after /api/ is the FPL endpoint, e.g. /bootstrap-static/
    const fplPath = url.pathname.replace(/^\/?/, '');

    if (!fplPath) {
      return new Response('Provide an FPL path, e.g. /bootstrap-static/', { status: 400 });
    }

    const fplUrl = FPL_BASE + fplPath + url.search;

    // Check Cloudflare cache
    const cacheKey = new Request(fplUrl);
    const cache = caches.default;
    let cached = await cache.match(cacheKey);
    if (cached) {
      const resp = new Response(cached.body, cached);
      resp.headers.set('Access-Control-Allow-Origin', '*');
      resp.headers.set('X-Cache', 'HIT');
      return resp;
    }

    const ttl = cacheTTL(fplPath);

    const upstream = await fetch(fplUrl, {
      headers: {
        'User-Agent': 'Mozilla/5.0 (compatible; FPL-proxy)',
        'Accept': 'application/json',
      },
      cf: { cacheTtl: ttl, cacheEverything: true },
    });

    if (!upstream.ok) {
      return new Response(`FPL API error: ${upstream.status}`, {
        status: upstream.status,
        headers: CORS_HEADERS,
      });
    }

    const body = await upstream.text();
    const response = new Response(body, {
      status: 200,
      headers: {
        ...CORS_HEADERS,
        'Content-Type': 'application/json',
        'Cache-Control': `public, max-age=${ttl}`,
        'X-Cache': 'MISS',
      },
    });

    ctx.waitUntil(cache.put(cacheKey, response.clone()));
    return response;
  },
};
