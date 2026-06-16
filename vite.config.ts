import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// The FinLedger backend ships no CORS middleware, so a browser can't call it
// cross-origin. In dev we proxy everything under /api to the backend and strip
// the prefix, so the app talks same-origin and CORS never applies:
//   /api/v1/accounts  ->  http://localhost:8000/v1/accounts
//   /api/health       ->  http://localhost:8000/health
//
// The dev proxy ALSO injects the API key (read from a non-VITE env var, so it is
// never bundled into the client) — mirroring the Vercel edge function in prod.
// The browser therefore never holds the key, in dev or in production.
export default defineConfig(({ mode }) => {
  // Empty prefix loads ALL env vars (including non-VITE_ ones) for Node-side use
  // here in the config. These are NOT exposed to the browser bundle.
  const env = loadEnv(mode, process.cwd(), '')
  const BACKEND = env.FINLEDGER_API_URL ?? 'http://localhost:8000'
  const API_KEY = env.FINLEDGER_API_KEY ?? ''

  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        '/api': {
          target: BACKEND,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, ''),
          headers: API_KEY ? { 'X-API-Key': API_KEY } : undefined,
        },
      },
    },
  }
})
