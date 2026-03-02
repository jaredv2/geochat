/**
 * GeoChat — Discord OAuth Proxy
 * Handles both token exchange AND user profile fetch.
 * Routes:
 *   POST /token  → Discord token exchange
 *   GET  /me     → Discord GET /users/@me
 */

Deno.serve(async (req: Request) => {
  const url = new URL(req.url);

  // Health check
  if (req.method === "GET" && url.pathname === "/") {
    return new Response(JSON.stringify({ ok: true, service: "geochat-discord-proxy" }), {
      headers: { "Content-Type": "application/json" },
    });
  }

  // Verify shared secret
  const secret = Deno.env.get("PROXY_SECRET") ?? "";
  if (secret && req.headers.get("X-Proxy-Secret") !== secret) {
    return new Response(JSON.stringify({ error: "forbidden" }), {
      status: 403, headers: { "Content-Type": "application/json" },
    });
  }

  // POST /token — OAuth token exchange
  if (req.method === "POST" && url.pathname === "/token") {
    const body = await req.text();
    try {
      const dr = await fetch("https://discord.com/api/v10/oauth2/token", {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "User-Agent": "DiscordBot (https://github.com/geochat, 1.0)",
        },
        body,
      });
      return new Response(await dr.text(), {
        status: dr.status,
        headers: { "Content-Type": dr.headers.get("Content-Type") ?? "application/json" },
      });
    } catch (e) {
      return new Response(JSON.stringify({ error: String(e) }), {
        status: 502, headers: { "Content-Type": "application/json" },
      });
    }
  }

  // GET /me — fetch user profile (forwards Bearer token)
  if (req.method === "GET" && url.pathname === "/me") {
    try {
      const dr = await fetch("https://discord.com/api/v10/users/@me", {
        headers: {
          "Authorization": req.headers.get("Authorization") ?? "",
          "User-Agent": "DiscordBot (https://github.com/geochat, 1.0)",
        },
      });
      return new Response(await dr.text(), {
        status: dr.status,
        headers: { "Content-Type": dr.headers.get("Content-Type") ?? "application/json" },
      });
    } catch (e) {
      return new Response(JSON.stringify({ error: String(e) }), {
        status: 502, headers: { "Content-Type": "application/json" },
      });
    }
  }

  return new Response(JSON.stringify({ error: "not found" }), {
    status: 404, headers: { "Content-Type": "application/json" },
  });
});
