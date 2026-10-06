import { initializeTelegramWebApp } from './telegram'

const AUTH_TIMEOUT_MS = 15_000

export type AuthenticatedUser = {
  id: number
  first_name: string
  last_name?: string
  username?: string
  language_code?: string
  is_premium: boolean
}

export type AuthenticationState =
  | { status: 'checking' }
  | { status: 'authenticated'; user: AuthenticatedUser }
  | { status: 'unavailable'; message: string }
  | { status: 'rejected'; message: string }
  | { status: 'error'; message: string }

export type AuthDiagnostic = {
  id: number
  time: string
  step: string
  message: string
  details?: Record<string, string | number | boolean | null>
}

let authenticationPromise: Promise<AuthenticationState> | null = null
let diagnosticSequence = 0
let diagnostics: AuthDiagnostic[] = []
const diagnosticListeners = new Set<(entries: AuthDiagnostic[]) => void>()

function addDiagnostic(
  step: string,
  message: string,
  details?: Record<string, string | number | boolean | null>,
) {
  diagnostics = [
    ...diagnostics.slice(-39),
    {
      id: ++diagnosticSequence,
      time: new Date().toISOString(),
      step,
      message,
      details,
    },
  ]
  diagnosticListeners.forEach((listener) => listener([...diagnostics]))
}

export function getAuthDiagnostics(): AuthDiagnostic[] {
  return [...diagnostics]
}

export function subscribeAuthDiagnostics(
  listener: (entries: AuthDiagnostic[]) => void,
): () => void {
  diagnosticListeners.add(listener)
  listener([...diagnostics])
  return () => diagnosticListeners.delete(listener)
}

function summarizeInitData(initData: string) {
  const params = new URLSearchParams(initData)
  const authDate = Number(params.get('auth_date'))
  const now = Math.floor(Date.now() / 1000)
  return {
    length: initData.length,
    fields: [...new Set(params.keys())].sort().join(', ') || '(none)',
    hasHash: Boolean(params.get('hash')),
    hasSignature: Boolean(params.get('signature')),
    hasUser: Boolean(params.get('user')),
    hasQueryId: Boolean(params.get('query_id')),
    authDateValid: Number.isFinite(authDate) && authDate > 0,
    authAgeSeconds: Number.isFinite(authDate) && authDate > 0 ? now - authDate : -1,
  }
}

async function responseDetail(response: Response): Promise<string | null> {
  try {
    const payload = await response.clone().json() as { detail?: unknown }
    if (typeof payload.detail === 'string') return payload.detail.slice(0, 200)
    if (payload.detail && typeof payload.detail === 'object') {
      return JSON.stringify(payload.detail).slice(0, 200)
    }
  } catch {
    // A missing JSON body is recorded as null instead of exposing response text.
  }
  return null
}

function isUser(value: unknown): value is AuthenticatedUser {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const user = value as Record<string, unknown>
  return (
    typeof user.id === 'number' &&
    Number.isInteger(user.id) &&
    user.id > 0 &&
    typeof user.first_name === 'string' &&
    user.first_name.length > 0 &&
    typeof user.is_premium === 'boolean'
  )
}

async function fetchAuth(url: string, init: RequestInit = {}): Promise<Response> {
  const controller = new AbortController()
  const timeoutId = window.setTimeout(() => controller.abort(), AUTH_TIMEOUT_MS)
  try {
    return await fetch(url, {
      ...init,
      credentials: 'same-origin',
      cache: 'no-store',
      signal: controller.signal,
    })
  } finally {
    window.clearTimeout(timeoutId)
  }
}

async function parseAuthenticatedUser(response: Response): Promise<AuthenticatedUser | null> {
  try {
    const payload = await response.json() as { user?: unknown }
    return isUser(payload.user) ? payload.user : null
  } catch {
    return null
  }
}

async function performAuthentication(): Promise<AuthenticationState> {
  const webApp = initializeTelegramWebApp()
  addDiagnostic('bootstrap', 'Authentication bootstrap started.', {
    pageOrigin: window.location.origin,
    pagePath: window.location.pathname,
    telegramObjectPresent: Boolean(window.Telegram),
    webAppPresent: Boolean(webApp),
    platform: webApp?.platform ?? 'unknown',
    version: webApp?.version ?? 'unknown',
  })

  try {
    const initData = webApp?.initData?.trim()
    if (initData) {
      addDiagnostic('init-data', 'Telegram initData detected.', summarizeInitData(initData))
      const startedAt = performance.now()
      addDiagnostic('auth-request', 'Sending POST /api/auth.', {
        credentials: 'same-origin',
        bodyBytes: new Blob([JSON.stringify({ init_data: initData })]).size,
      })
      const response = await fetchAuth('/api/auth', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ init_data: initData }),
      })
      addDiagnostic('auth-response', 'Received POST /api/auth response.', {
        status: response.status,
        ok: response.ok,
        redirected: response.redirected,
        responseUrl: response.url,
        contentType: response.headers.get('content-type'),
        authErrorCode: response.headers.get('x-telegram-auth-error'),
        detail: await responseDetail(response),
        elapsedMs: Math.round(performance.now() - startedAt),
      })
      if (response.status === 401 || response.status === 403) {
        return {
          status: 'rejected',
          message: 'Your Telegram launch has expired or could not be verified. Reopen the video from the bot.',
        }
      }
      if (!response.ok) {
        return { status: 'error', message: 'Telegram authentication is temporarily unavailable.' }
      }

      const user = await parseAuthenticatedUser(response)
      addDiagnostic('auth-parse', user
        ? 'Authenticated user response parsed successfully.'
        : 'Authentication succeeded but the user response was invalid.', {
        userPresent: Boolean(user),
      })
      return user
        ? { status: 'authenticated', user }
        : { status: 'error', message: 'The server returned an invalid authentication response.' }
    }

    addDiagnostic('init-data', 'No Telegram initData was available.', {
      webAppPresent: Boolean(webApp),
      initDataLength: webApp?.initData?.length ?? 0,
    })
    const sessionStartedAt = performance.now()
    addDiagnostic('session-request', 'Sending GET /api/auth/me.')
    const currentSession = await fetchAuth('/api/auth/me')
    addDiagnostic('session-response', 'Received GET /api/auth/me response.', {
      status: currentSession.status,
      ok: currentSession.ok,
      detail: await responseDetail(currentSession),
      elapsedMs: Math.round(performance.now() - sessionStartedAt),
    })
    if (currentSession.ok) {
      const user = await parseAuthenticatedUser(currentSession)
      if (user) return { status: 'authenticated', user }
      return { status: 'error', message: 'The server returned an invalid session.' }
    }
    if (currentSession.status === 401) {
      return {
        status: 'unavailable',
        message: 'Open this video using the button inside the Telegram bot.',
      }
    }
    return { status: 'error', message: 'Could not check your Telegram session.' }
  } catch (error) {
    addDiagnostic('exception', 'Authentication request raised an exception.', {
      type: error instanceof Error ? error.name : typeof error,
      message: error instanceof Error ? error.message.slice(0, 200) : 'Unknown error',
      online: navigator.onLine,
    })
    const message = error instanceof DOMException && error.name === 'AbortError'
      ? 'Telegram authentication took too long. Please try again.'
      : 'Could not reach the authentication service. Check your connection.'
    return { status: 'error', message }
  }
}

export function authenticateTelegramSession(force = false): Promise<AuthenticationState> {
  if (force) {
    authenticationPromise = null
    addDiagnostic('retry', 'Manual authentication retry requested.')
  }
  authenticationPromise ??= performAuthentication()
  return authenticationPromise
}
