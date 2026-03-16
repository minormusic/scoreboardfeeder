/**
 * Taso API Proxy — Cloudflare Worker
 * Välittää pyynnöt spl.torneopal.net:lle Cloudflaren kautta.
 *
 * Käyttö: https://taso-proxy.santtusipila.workers.dev/getMatches?venue_id=325&date=2026-03-16&api_key=xxx
 */

const TARGET = "https://spl.torneopal.net/taso/rest";

export default {
  async fetch(request) {
    const url = new URL(request.url);

    // Terveystarkistus
    if (url.pathname === "/" || url.pathname === "/health") {
      return new Response(JSON.stringify({ status: "ok", proxy: "taso-proxy" }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    // Välitä pyyntö Taso API:lle
    const targetUrl = TARGET + url.pathname + url.search;

    const resp = await fetch(targetUrl, {
      method: request.method,
      headers: {
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-US,en;q=0.9,fi;q=0.8",
        "Referer": "https://tulospalvelu.palloliitto.fi/",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
      },
    });

    // Palauta vastaus CORS-headereilla
    const body = await resp.text();
    return new Response(body, {
      status: resp.status,
      headers: {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
      },
    });
  },
};
