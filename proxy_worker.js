/**
 * GeoChat — Discord OAuth Token Proxy
 * Cloudflare Worker (Service Worker format — works with drag-and-drop deploy)
 *
 * HOW TO DEPLOY:
 *   1. Go to dash.cloudflare.com → Workers & Pages → Create → Create Worker
 *   2. Click "Edit Code" on the next screen
 *   3. Delete ALL existing code and paste this entire file
 *   4. Click Save and Deploy
 *   5. Copy your worker URL (e.g. https://geochat-proxy.yourname.workers.dev)
 *   6. Add to Render env vars:
 *        DISCORD_PROXY_URL = https://geochat-proxy.nrm77.workers.dev
 *        DISCORD_PROXY_SECRET = (any random string you choose)
 *   7. Add to Worker env vars (Worker Settings → Variables):
 *        PROXY_SECRET = (same random string)
 */

addEventListener('fetch', event => {
  event.respondWith(handleRequest(event.request))
})

async function handleRequest(request) {
  // Only allow POST to /token
  if (request.method !== 'POST') {
    return new Response('Method not allowed', { status: 405 })
  }

  const url = new URL(request.url)
  if (url.pathname !== '/token') {
    return new Response('Not found', { status: 404 })
  }

  // Optional shared secret check (set PROXY_SECRET in Worker env vars)
  const secret = typeof PROXY_SECRET !== 'undefined' ? PROXY_SECRET : ''
  if (secret && request.headers.get('X-Proxy-Secret') !== secret) {
    return new Response('Forbidden', { status: 403 })
  }

  // Forward body to Discord as-is
  const body = await request.text()

  const discordResp = await fetch('https://discord.com/api/v10/oauth2/token', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      'User-Agent': 'GeoChat/1.0',
    },
    body: body,
  })

  const respText = await discordResp.text()

  return new Response(respText, {
    status: discordResp.status,
    headers: {
      'Content-Type': discordResp.headers.get('Content-Type') || 'application/json',
    },
  })
}
