/**
 * CORS proxy for the LLM Gateway benchmarks endpoint.
 *
 * The upstream API only sends `access-control-allow-origin: https://llmgateway.io`,
 * so a browser on github.io cannot read it directly. This Worker fetches the data
 * server-side and re-serves it with permissive CORS, which is what makes the
 * dashboard update live instead of waiting for the 5-minute Actions cron.
 *
 * Deploy by pasting into the Cloudflare dashboard editor — no build step needed.
 */

const MODEL = "deepseek-v4-flash";
const UPSTREAM = `https://internal.llmgateway.io/internal/models/${MODEL}/benchmarks`;

// Upstream moves about every 10s; a short edge cache keeps us far away from
// both the Workers free-tier limit and any upstream rate limiting.
const CACHE_SECONDS = 5;

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, OPTIONS",
  "cache-control": `public, max-age=${CACHE_SECONDS}`,
};

export default {
  async fetch(request) {
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: CORS });
    }

    try {
      const upstream = await fetch(UPSTREAM, {
        headers: { accept: "application/json", "user-agent": "llmgateway-trends" },
        cf: { cacheTtl: CACHE_SECONDS, cacheEverything: true },
      });

      if (!upstream.ok) {
        return json({ error: `upstream ${upstream.status}` }, 502);
      }

      const providers = (await upstream.json()).providers || [];

      return json({
        ts: new Date().toISOString().replace(/\.\d+Z$/, "+00:00"),
        windowHours: providers.length ? providers[0].windowHours : null,
        providers: providers.map((p) => ({
          id: p.providerId,
          name: p.providerName || p.providerId,
          requests: p.logsCount || 0,
          errors: p.errorsCount || 0,
          cached: p.cachedCount || 0,
          errorRate: p.errorRate,
          uptime: p.uptime,
          tps: p.tokensPerSecond,
          ttft: p.avgTimeToFirstToken,
        })),
      });
    } catch (err) {
      return json({ error: String(err) }, 502);
    }
  },
};

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "content-type": "application/json; charset=utf-8" },
  });
}
