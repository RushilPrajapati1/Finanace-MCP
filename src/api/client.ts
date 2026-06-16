// Thin typed wrapper over the FinLedger HTTP API.
//
// All requests go to `${baseUrl}` which defaults to "/api" — the Vite dev proxy
// forwards that to the backend (see vite.config.ts), so the browser stays
// same-origin and never trips the backend's missing CORS headers.

import type {
  Account,
  AccountCreate,
  Balance,
  Health,
  Integrity,
  ReversalCreate,
  Transaction,
  TransactionCreate,
  TrialBalance,
} from './types'

const API_KEY_STORAGE = 'finledger.apiKey'
const BASE_URL_STORAGE = 'finledger.baseUrl'
const DEFAULT_BASE_URL = '/api'

// The API key is NOT held in the browser. When the app talks to the default
// "/api" base, a server-side proxy injects the key for every request:
//   - in production, the Vercel edge function in web/api/[...path].ts
//   - in dev, the Vite proxy in vite.config.ts
// Both read the key from a server-only env var (FINLEDGER_API_KEY), so it is
// never bundled into the client. A browser key (Settings) is only needed when
// the user points the app directly at a custom backend URL, bypassing the proxy.

export class ApiError extends Error {
  code: string
  status: number
  constructor(code: string, message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
  }
}

export function getApiKey(): string {
  return localStorage.getItem(API_KEY_STORAGE) ?? ''
}

/** True when requests go through the key-injecting proxy (the default base). */
export function usesServerKey(): boolean {
  return getBaseUrl() === DEFAULT_BASE_URL
}

export function setApiKey(key: string): void {
  if (key) localStorage.setItem(API_KEY_STORAGE, key)
  else localStorage.removeItem(API_KEY_STORAGE)
}

export function getBaseUrl(): string {
  return localStorage.getItem(BASE_URL_STORAGE) ?? DEFAULT_BASE_URL
}

export function setBaseUrl(url: string): void {
  if (url && url !== DEFAULT_BASE_URL) localStorage.setItem(BASE_URL_STORAGE, url)
  else localStorage.removeItem(BASE_URL_STORAGE)
}

/**
 * Whether the app can make authenticated calls: either the proxy injects the
 * key (default), or a browser key is set for a direct/custom backend.
 */
export function hasApiKey(): boolean {
  return usesServerKey() || getApiKey().trim().length > 0
}

/** Stable client-side idempotency key for transaction POSTs. */
export function newIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID()
  }
  return `idem-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

async function request<T>(
  path: string,
  options: { method?: string; body?: unknown; auth?: boolean } = {},
): Promise<T> {
  const { method = 'GET', body, auth = true } = options
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (auth && !usesServerKey()) {
    // Direct/custom-backend mode: the browser must supply the key itself.
    // In the default proxy mode the server injects it, so we send nothing.
    const key = getApiKey()
    if (!key) throw new ApiError('no_api_key', 'No API key configured.', 0)
    headers['X-API-Key'] = key
  }

  let res: Response
  try {
    res = await fetch(`${getBaseUrl()}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch {
    throw new ApiError(
      'network_error',
      'Could not reach the ledger. Is the backend running on :8000?',
      0,
    )
  }

  if (res.status === 204) return undefined as T

  let payload: unknown = null
  const text = await res.text()
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = text
    }
  }

  if (!res.ok) {
    const err = (payload as { error?: { code?: string; message?: string } })?.error
    throw new ApiError(
      err?.code ?? 'http_error',
      err?.message ?? `Request failed (${res.status}).`,
      res.status,
    )
  }
  return payload as T
}

export const api = {
  // health (no /v1 prefix, no auth)
  health: () => request<Health>('/health', { auth: false }),

  // accounts
  listAccounts: () => request<Account[]>('/v1/accounts?limit=500'),
  getAccount: (id: string) => request<Account>(`/v1/accounts/${id}`),
  createAccount: (body: AccountCreate) =>
    request<Account>('/v1/accounts', { method: 'POST', body }),
  getBalance: (id: string) => request<Balance>(`/v1/accounts/${id}/balance`),

  // transactions
  listTransactions: (limit = 100) =>
    request<Transaction[]>(`/v1/transactions?limit=${limit}`),
  getTransaction: (id: string) => request<Transaction>(`/v1/transactions/${id}`),
  createTransaction: (body: TransactionCreate) =>
    request<Transaction>('/v1/transactions', { method: 'POST', body }),
  reverseTransaction: (id: string, body: ReversalCreate) =>
    request<Transaction>(`/v1/transactions/${id}/reversal`, {
      method: 'POST',
      body,
    }),

  // ledger
  trialBalance: () => request<TrialBalance>('/v1/ledger/trial-balance'),
  verify: () => request<Integrity>('/v1/ledger/verify'),
}
