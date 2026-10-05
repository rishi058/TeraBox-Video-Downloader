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

let authenticationPromise: Promise<AuthenticationState> | null = null

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

  try {
    const currentSession = await fetchAuth('/api/auth/me')
    if (currentSession.ok) {
      const user = await parseAuthenticatedUser(currentSession)
      if (user) return { status: 'authenticated', user }
      return { status: 'error', message: 'The server returned an invalid session.' }
    }
    if (currentSession.status !== 401) {
      return { status: 'error', message: 'Could not check your Telegram session.' }
    }

    const initData = webApp?.initData?.trim()
    if (!initData) {
      return {
        status: 'unavailable',
        message: 'Open this video using the button inside the Telegram bot.',
      }
    }

    const response = await fetchAuth('/api/auth', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ init_data: initData }),
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
    return user
      ? { status: 'authenticated', user }
      : { status: 'error', message: 'The server returned an invalid authentication response.' }
  } catch (error) {
    const message = error instanceof DOMException && error.name === 'AbortError'
      ? 'Telegram authentication took too long. Please try again.'
      : 'Could not reach the authentication service. Check your connection.'
    return { status: 'error', message }
  }
}

export function authenticateTelegramSession(force = false): Promise<AuthenticationState> {
  if (force) authenticationPromise = null
  authenticationPromise ??= performAuthentication()
  return authenticationPromise
}
