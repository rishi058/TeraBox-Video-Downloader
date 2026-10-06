export type TelegramWebApp = {
  initData: string
  ready: () => void
  expand: () => void
  version?: string
  platform?: string
  colorScheme?: 'light' | 'dark'
}

declare global {
  interface Window {
    Telegram?: {
      WebApp?: TelegramWebApp
    }
  }
}

export function getTelegramWebApp(): TelegramWebApp | null {
  const webApp = window.Telegram?.WebApp
  if (!webApp || typeof webApp.ready !== 'function' || typeof webApp.expand !== 'function') {
    return null
  }
  return webApp
}

export function initializeTelegramWebApp(): TelegramWebApp | null {
  const webApp = getTelegramWebApp()
  if (!webApp) return null
  webApp.ready()
  webApp.expand()
  return webApp
}
