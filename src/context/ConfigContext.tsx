import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import {
  getApiKey,
  getBaseUrl,
  hasApiKey,
  setApiKey as persistKey,
  setBaseUrl as persistBaseUrl,
} from '../api/client'

interface ConfigValue {
  apiKey: string
  baseUrl: string
  configured: boolean
  setApiKey: (key: string) => void
  setBaseUrl: (url: string) => void
}

const ConfigContext = createContext<ConfigValue | null>(null)

export function ConfigProvider({ children }: { children: ReactNode }) {
  const [apiKey, setApiKeyState] = useState(getApiKey)
  const [baseUrl, setBaseUrlState] = useState(getBaseUrl)

  const setApiKey = useCallback((key: string) => {
    persistKey(key.trim())
    setApiKeyState(key.trim())
  }, [])

  const setBaseUrl = useCallback((url: string) => {
    persistBaseUrl(url.trim())
    setBaseUrlState(getBaseUrl())
  }, [])

  const value = useMemo<ConfigValue>(
    () => ({
      apiKey,
      baseUrl,
      // Configured when the proxy injects the key (default) or a browser key is
      // set for a direct/custom backend. `baseUrl`/`apiKey` are in the deps so
      // this recomputes when either changes.
      configured: hasApiKey(),
      setApiKey,
      setBaseUrl,
    }),
    [apiKey, baseUrl, setApiKey, setBaseUrl],
  )

  return <ConfigContext.Provider value={value}>{children}</ConfigContext.Provider>
}

export function useConfig(): ConfigValue {
  const ctx = useContext(ConfigContext)
  if (!ctx) throw new Error('useConfig must be used within ConfigProvider')
  return ctx
}
