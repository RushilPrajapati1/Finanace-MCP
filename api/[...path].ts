// Server-side proxy (Backend-for-Frontend) for the FinLedger API.
//
// The browser calls this function under /api/* with NO credentials. The function
// runs on Vercel's edge runtime, injects the tenant API key from a SERVER-ONLY
// env var (FINLEDGER_API_KEY — never a VITE_ var, so it is never bundled into the
// client), and forwards the request to the Render backend. This is what keeps the
// secret key off the browser entirely.
//
// Required Vercel env vars (Project → Settings → Environment Variables):
//   FINLEDGER_API_KEY   the rotated tenant key (sk_live_...)   [secret]
//   FINLEDGER_API_URL   the backend base URL (optional; default below)

export const config = { runtime: 'edge' }

const UPSTREAM = (process.env.FINLEDGER_API_URL ?? 'https://finledger-api-rw1f.onrender.com').replace(/\/$/, '')
const API_KEY = process.env.FINLEDGER_API_KEY ?? ''

export default async function handler(req: Request): Promise<Response> {
  if (!API_KEY) {
    return errorJson(500, 'gateway_misconfigured', 'FINLEDGER_API_KEY is not set on the server.')
  }

  const url = new URL(req.url)
  // The SPA uses "/api" as its base; strip it to get the real ledger path.
  const path = url.pathname.replace(/^\/api/, '') || '/'
  const target = `${UPSTREAM}${path}${url.search}`

  const headers = new Headers(req.headers)
  // The browser must never supply credentials; the server owns the only key.
  headers.delete('x-api-key')
  headers.delete('authorization')
  headers.delete('cookie')
  headers.delete('host')
  // Force identity encoding so the upstream body can be streamed back verbatim.
  headers.delete('accept-encoding')
  headers.set('X-API-Key', API_KEY)

  const hasBody = req.method !== 'GET' && req.method !== 'HEAD'

  let upstream: Response
  try {
    upstream = await fetch(target, {
      method: req.method,
      headers,
      body: hasBody ? await req.arrayBuffer() : undefined,
    })
  } catch {
    return errorJson(502, 'upstream_unavailable', 'The ledger backend is unreachable.')
  }

  // Pass the ledger's response (status, body, content-type) straight through.
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: upstream.headers,
  })
}

// Match the ledger's error envelope so the client handles these identically.
function errorJson(status: number, code: string, message: string): Response {
  return new Response(JSON.stringify({ error: { code, message } }), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}
