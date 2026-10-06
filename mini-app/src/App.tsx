import { useEffect, useRef, useState } from 'react'
import {
  authenticateTelegramSession,
  getAuthDiagnostics,
  subscribeAuthDiagnostics,
  type AuthDiagnostic,
  type AuthenticationState,
} from './auth'
import './App.css'

const API_BASE_URL = 'http://dsm89t26p1mhjww7bkvldkwq.141.148.151.172.sslip.io'
const METADATA_ENDPOINT = '/v2/video'
const DECRYPTION_ENDPOINT = '/decrypt'
const API_KEY = 'f868d642d4f85459465462e09bfda7ec'
const REQUEST_TIMEOUT_MS = 30_000
const MAX_URL_LENGTH = 2_048

// Matches diskwala.com and its subdomains, but not look-alike domains.
const DISKWALA_HOST_PATTERN = /^(?:[a-z0-9-]+\.)*diskwala\.com$/i
const SAFE_FORWARD_HEADERS = new Set([
  'authorization',
  'content-type',
  'x-dw-kid',
  'x-dw-nonce',
  'x-dw-sig',
  'x-dw-ts',
])
type Route =
  | { name: 'missing' }
  | { name: 'invalid'; message: string }
  | { name: 'video'; url: string }

type RequestDescriptor = {
  method?: unknown
  url?: unknown
  req_headers?: unknown
  req_body?: unknown
}

type VideoInfo = {
  name: string
  type: string
  thumb?: string
  views?: number
  size?: number
  extension?: string
  url: string
}

type ProgressState = {
  percent: number
  title: string
  detail: string
}

type DiagnosticDetails = Record<string, string | number | boolean | null>

type VideoDiagnostic = {
  id: number
  time: string
  step: string
  message: string
  details?: DiagnosticDetails
}

type DiagnosticWriter = (
  step: string,
  message: string,
  details?: DiagnosticDetails,
) => void

const INITIAL_PROGRESS: ProgressState = {
  percent: 10,
  title: 'Checking your link',
  detail: 'Confirming that this is a valid DiskWala video.',
}

class AppError extends Error {
  code: string

  constructor(message: string, code = 'PROCESSING_ERROR') {
    super(message)
    this.name = 'AppError'
    this.code = code
  }
}

function parseRoute(search: string): Route {
  const rawUrl = new URLSearchParams(search).get('url')

  if (!rawUrl?.trim()) {
    return { name: 'missing' }
  }

  return sanitizeDiskWalaUrl(rawUrl)
}

function sanitizeDiskWalaUrl(value: string): Route {
  const trimmed = value.trim()
  const hasControlCharacter = [...trimmed].some((character) => {
    const codePoint = character.charCodeAt(0)
    return codePoint <= 31 || codePoint === 127
  })

  if (trimmed.length > MAX_URL_LENGTH || hasControlCharacter) {
    return { name: 'invalid', message: 'The supplied URL contains unsupported characters.' }
  }

  try {
    const parsed = new URL(trimmed)
    const isDefaultPort = parsed.port === '' || parsed.port === '443'

    if (
      parsed.protocol !== 'https:' ||
      !DISKWALA_HOST_PATTERN.test(parsed.hostname) ||
      parsed.username ||
      parsed.password ||
      !isDefaultPort
    ) {
      return {
        name: 'invalid',
        message: 'Only secure links from diskwala.com are supported.',
      }
    }

    parsed.hash = ''
    return { name: 'video', url: parsed.toString() }
  } catch {
    return { name: 'invalid', message: 'The supplied value is not a valid URL.' }
  }
}

function assertSafeDiskWalaEndpoint(value: unknown): string {
  if (typeof value !== 'string') {
    throw new AppError('The server returned an incomplete request.', 'INVALID_SERVER_RESPONSE')
  }
  try {
    const parsed = new URL(value)
    if (
      parsed.protocol !== 'https:' ||
      !DISKWALA_HOST_PATTERN.test(parsed.hostname) ||
      parsed.username ||
      parsed.password ||
      (parsed.port !== '' && parsed.port !== '443')
    ) {
      throw new Error('Unsafe target')
    }
    return parsed.toString()
  } catch {
    throw new AppError('The server returned an unsafe video endpoint.', 'UNSAFE_ENDPOINT')
  }
}

function sanitizeHeaders(value: unknown): Headers {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new AppError('The server did not provide request headers.', 'INVALID_SERVER_RESPONSE')
  }
  const headers = new Headers()
  Object.entries(value).forEach(([name, headerValue]) => {
    const normalizedName = name.toLowerCase()
    if (SAFE_FORWARD_HEADERS.has(normalizedName) && typeof headerValue === 'string') {
      headers.set(normalizedName, headerValue.replace(/[\r\n]/g, '').trim())
    }
  })
  if (!headers.has('authorization')) {
    throw new AppError('The generated request is missing authorization.', 'INVALID_SERVER_RESPONSE')
  }
  if (!headers.has('content-type')) {
    headers.set('content-type', 'application/json; charset=utf-8')
  }
  return headers
}

async function fetchWithTimeout(
  url: string,
  init: RequestInit,
  parentSignal: AbortSignal,
  timeoutLabel: string,
  diagnostic: DiagnosticWriter,
): Promise<Response> {
  const startedAt = performance.now()
  let targetOrigin = 'invalid-url'
  let targetPath = url
  let targetProtocol = 'unknown'
  try {
    const parsedTarget = new URL(url, window.location.origin)
    targetOrigin = parsedTarget.origin
    targetPath = parsedTarget.pathname
    targetProtocol = parsedTarget.protocol
  } catch {
    // The request itself will provide the final invalid URL error.
  }
  diagnostic('request-start', `${timeoutLabel} request started.`, {
    method: init.method?.toString() ?? 'GET',
    targetOrigin,
    targetPath,
    targetProtocol,
    pageOrigin: window.location.origin,
    pageProtocol: window.location.protocol,
    mixedContent: window.location.protocol === 'https:' && targetProtocol === 'http:',
    online: navigator.onLine,
  })
  const controller = new AbortController()
  const abortFromParent = () => controller.abort(parentSignal.reason)
  const timeoutId = window.setTimeout(
    () => controller.abort(new DOMException(`${timeoutLabel} timed out`, 'TimeoutError')),
    REQUEST_TIMEOUT_MS,
  )

  if (parentSignal.aborted) {
    abortFromParent()
  } else {
    parentSignal.addEventListener('abort', abortFromParent, { once: true })
  }

  try {
    const response = await fetch(url, {
      ...init,
      credentials: 'same-origin',
      cache: 'no-store',
      signal: controller.signal,
    })
    diagnostic('request-response', `${timeoutLabel} returned an HTTP response.`, {
      status: response.status,
      statusText: response.statusText || '(empty)',
      ok: response.ok,
      redirected: response.redirected,
      responseOrigin: new URL(response.url).origin,
      responsePath: new URL(response.url).pathname,
      responseType: response.type,
      contentType: response.headers.get('content-type'),
      elapsedMs: Math.round(performance.now() - startedAt),
    })
    return response
  } catch (error) {
    if (parentSignal.aborted) {
      throw error
    }
    if (controller.signal.reason instanceof DOMException && controller.signal.reason.name === 'TimeoutError') {
      diagnostic('request-error', `${timeoutLabel} timed out.`, {
        errorType: 'TimeoutError',
        elapsedMs: Math.round(performance.now() - startedAt),
        targetOrigin,
      })
      throw new AppError(`${timeoutLabel} took too long. Please try again.`, 'TIMEOUT')
    }
    diagnostic('request-error', `${timeoutLabel} failed before an HTTP response was available.`, {
      errorType: error instanceof Error ? error.name : typeof error,
      errorMessage: error instanceof Error ? error.message.slice(0, 240) : 'Unknown error',
      elapsedMs: Math.round(performance.now() - startedAt),
      targetOrigin,
      targetProtocol,
      mixedContent: window.location.protocol === 'https:' && targetProtocol === 'http:',
      online: navigator.onLine,
      likelyCauses: 'CORS, TLS certificate, mixed content, DNS, or unreachable host',
    })
    throw new AppError(
      'Could not reach the video service. Check your connection and try again.',
      'NETWORK_ERROR',
    )
  } finally {
    window.clearTimeout(timeoutId)
    parentSignal.removeEventListener('abort', abortFromParent)
  }
}

async function requireOk(response: Response, context: string): Promise<Response> {
  if (response.ok) {
    return response
  }

  if (response.status === 404) {
    throw new AppError('This video could not be found or is no longer available.', 'VIDEO_NOT_FOUND')
  }
  if (response.status === 401 || response.status === 403) {
    throw new AppError('The video service rejected this request. Please try again.', 'ACCESS_DENIED')
  }
  if (response.status === 429) {
    throw new AppError('Too many requests were made. Wait a moment and try again.', 'RATE_LIMITED')
  }
  if (response.status >= 500) {
    throw new AppError('The video service is temporarily unavailable. Please try again.', 'SERVICE_ERROR')
  }

  throw new AppError(`${context} failed (HTTP ${response.status}).`, 'REQUEST_FAILED')
}

async function parseJsonResponse(response: Response, context: string): Promise<unknown> {
  const text = await response.text()
  if (!text) {
    throw new AppError(`${context} returned an empty response.`, 'INVALID_SERVER_RESPONSE')
  }

  try {
    return JSON.parse(text) as unknown
  } catch {
    throw new AppError(`${context} returned an unreadable response.`, 'INVALID_SERVER_RESPONSE')
  }
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function parseNestedJson(value: unknown): unknown {
  if (typeof value !== 'string') {
    return value
  }
  try {
    return JSON.parse(value) as unknown
  } catch {
    return value
  }
}

function extractEncryptedBody(value: unknown): Record<string, unknown> {
  const topLevel = asRecord(value)
  const responseBody = parseNestedJson(topLevel?.resp_body ?? value)
  const responseRecord = asRecord(responseBody)
  const fileInfo = responseRecord?.fileInfo ?? topLevel?.fileInfo
  if (typeof fileInfo !== 'string' || fileInfo.length < 16) {
    throw new AppError('The video service did not return encrypted video details.', 'INVALID_VIDEO_DATA')
  }
  return responseRecord ?? topLevel ?? { fileInfo }
}

function safeAssetUrl(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined
  try {
    const parsed = new URL(value, window.location.origin)
    if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') return undefined
    if (parsed.origin !== window.location.origin && parsed.protocol !== 'https:') return undefined
    return parsed.toString()
  } catch {
    return undefined
  }
}

function normalizeVideoInfo(value: unknown): VideoInfo {
  const topLevel = asRecord(value)
  const candidate = asRecord(parseNestedJson(topLevel?.resp_body ?? topLevel?.data ?? value))

  if (!candidate) {
    throw new AppError('The decrypted video details are invalid.', 'INVALID_VIDEO_DATA')
  }

  const downloadUrl = safeAssetUrl(candidate.url)
  if (!downloadUrl) {
    throw new AppError('A secure download link was not returned.', 'INVALID_VIDEO_DATA')
  }

  const name = typeof candidate.name === 'string' && candidate.name.trim()
    ? candidate.name.trim().slice(0, 240)
    : 'DiskWala video'

  return {
    name,
    type: typeof candidate.type === 'string' ? candidate.type : 'video/mp4',
    thumb: safeAssetUrl(candidate.thumb),
    views: typeof candidate.views === 'number' && Number.isFinite(candidate.views)
      ? candidate.views
      : undefined,
    size: typeof candidate.size === 'number' && Number.isFinite(candidate.size)
      ? candidate.size
      : undefined,
    extension: typeof candidate.extension === 'string'
      ? candidate.extension.replace(/[^a-z0-9]/gi, '').slice(0, 10)
      : undefined,
    url: downloadUrl,
  }
}

async function resolveVideo(
  sourceUrl: string,
  signal: AbortSignal,
  onProgress: (progress: ProgressState) => void,
  diagnostic: DiagnosticWriter,
): Promise<VideoInfo> {
  let sourceHost = 'invalid-url'
  try {
    sourceHost = new URL(sourceUrl).hostname
  } catch {
    // URL validation reports the user-facing error elsewhere.
  }
  diagnostic('video-flow', 'Video processing started.', {
    sourceHost,
    apiBase: API_BASE_URL,
    apiKeyConfigured: Boolean(API_KEY),
    browserOrigin: window.location.origin,
    browserOnline: navigator.onLine,
  })
  onProgress(INITIAL_PROGRESS)
  const metadataResponse = await fetchWithTimeout(
    `${API_BASE_URL}${METADATA_ENDPOINT}`,
    {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-api-key': API_KEY },
      body: JSON.stringify({ url: sourceUrl }),
    },
    signal,
    'Link check',
    diagnostic,
  )
  await requireOk(metadataResponse, 'Link check')
  const descriptorValue = await parseJsonResponse(metadataResponse, 'Link check')
  const descriptor = asRecord(descriptorValue) as RequestDescriptor | null
  diagnostic('metadata-parse', 'Metadata response parsed.', {
    responseIsObject: Boolean(descriptor),
    responseFields: descriptor ? Object.keys(descriptor).sort().join(', ') : '(not an object)',
    method: typeof descriptor?.method === 'string' ? descriptor.method : '(missing)',
    targetUrlPresent: typeof descriptor?.url === 'string',
    requestHeadersPresent: Boolean(descriptor?.req_headers),
    requestBodyType: typeof descriptor?.req_body,
  })
  if (!descriptor || (typeof descriptor.method === 'string' && descriptor.method.toUpperCase() !== 'POST')) {
    throw new AppError('The server returned an invalid request method.', 'INVALID_SERVER_RESPONSE')
  }

  const targetUrl = assertSafeDiskWalaEndpoint(descriptor.url)
  const targetHeaders = sanitizeHeaders(descriptor.req_headers)
  const requestBody = typeof descriptor.req_body === 'string'
    ? descriptor.req_body
    : JSON.stringify(descriptor.req_body ?? {})
  const target = new URL(targetUrl)
  diagnostic('provider-request', 'Prepared direct browser request to DiskWala provider.', {
    targetOrigin: target.origin,
    targetPath: target.pathname,
    forwardedHeaders: [...targetHeaders.keys()].sort().join(', '),
    requestBodyBytes: new Blob([requestBody]).size,
    clientIpUsed: true,
  })

  onProgress({
    percent: 62,
    title: 'Fetching video details',
    detail: 'Connecting directly to DiskWala from your device.',
  })
  const encryptedResponse = await fetchWithTimeout(
    targetUrl,
    { method: 'POST', headers: targetHeaders, body: requestBody },
    signal,
    'Video request',
    diagnostic,
  )
  await requireOk(encryptedResponse, 'Video request')
  const encryptedPayload = await parseJsonResponse(encryptedResponse, 'Video request')
  diagnostic('provider-parse', 'DiskWala provider response parsed.', {
    responseType: Array.isArray(encryptedPayload) ? 'array' : typeof encryptedPayload,
    responseFields: asRecord(encryptedPayload)
      ? Object.keys(asRecord(encryptedPayload)!).sort().join(', ')
      : '(not an object)',
  })
  const encryptedBody = extractEncryptedBody(encryptedPayload)

  onProgress({
    percent: 84,
    title: 'Unlocking video',
    detail: 'Decrypting the final file information.',
  })
  const decryptResponse = await fetchWithTimeout(
    `${API_BASE_URL}${DECRYPTION_ENDPOINT}`,
    {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-api-key': API_KEY },
      body: JSON.stringify({ encrypted_body: encryptedBody }),
    },
    signal,
    'Decryption',
    diagnostic,
  )
  await requireOk(decryptResponse, 'Decryption')
  const decryptedPayload = await parseJsonResponse(decryptResponse, 'Decryption')
  const video = normalizeVideoInfo(decryptedPayload)
  diagnostic('video-ready', 'Decrypted video metadata validated successfully.', {
    mediaType: video.type,
    hasThumbnail: Boolean(video.thumb),
    hasSize: video.size !== undefined,
    videoHost: new URL(video.url).hostname,
  })

  onProgress({
    percent: 100,
    title: 'Your video is ready',
    detail: 'The secure download link has been prepared.',
  })
  return video
}

function formatFileSize(bytes?: number): string | null {
  if (bytes === undefined || bytes < 0) return null
  if (bytes < 1_000) return `${bytes} B`
  const units = ['KB', 'MB', 'GB']
  let value = bytes / 1_000
  let unitIndex = 0
  while (value >= 1_000 && unitIndex < units.length - 1) {
    value /= 1_000
    unitIndex += 1
  }
  return `${value >= 10 ? value.toFixed(1) : value.toFixed(2)} ${units[unitIndex]}`
}

function VideoIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M8.2 5.8v12.4a1 1 0 0 0 1.55.83l8.65-6.2a1 1 0 0 0 0-1.66l-8.65-6.2a1 1 0 0 0-1.55.8Z" />
    </svg>
  )
}

function DownloadIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 3v12m0 0 5-5m-5 5-5-5M5 20h14" />
    </svg>
  )
}

function VideoPlayer({ video }: { video: VideoInfo }) {
  const playerRef = useRef<HTMLDivElement>(null)
  const videoRef = useRef<HTMLVideoElement>(null)
  const [playerError, setPlayerError] = useState(false)
  const [playbackRate, setPlaybackRate] = useState('1')

  const seekBy = (seconds: number) => {
    const element = videoRef.current
    if (!element || !Number.isFinite(element.duration)) return
    element.currentTime = Math.min(Math.max(element.currentTime + seconds, 0), element.duration)
  }

  const changePlaybackRate = (value: string) => {
    setPlaybackRate(value)
    if (videoRef.current) {
      videoRef.current.playbackRate = Number(value)
    }
  }

  const openPictureInPicture = async () => {
    const element = videoRef.current
    if (!element || !('requestPictureInPicture' in element)) return
    try {
      await element.requestPictureInPicture()
    } catch {
      // The browser can reject Picture in Picture when the video is not ready.
    }
  }

  const openFullscreen = async () => {
    if (!playerRef.current?.requestFullscreen) return
    try {
      await playerRef.current.requestFullscreen()
    } catch {
      // Fullscreen can be unavailable inside some Telegram webviews.
    }
  }

  return (
    <div className="player-section">
      <div className="video-player" ref={playerRef}>
        {!playerError ? (
          <video
            ref={videoRef}
            src={video.url}
            poster={video.thumb}
            controls
            controlsList="nodownload"
            playsInline
            preload="metadata"
            onError={() => setPlayerError(true)}
          >
            Your browser does not support HTML video playback.
          </video>
        ) : (
          <div className="player-error" role="status">
            <span className="state-icon error-icon" aria-hidden="true">!</span>
            <strong>Preview unavailable</strong>
            <p>This file cannot be streamed here, but you can still download it below.</p>
          </div>
        )}
      </div>

      {!playerError && (
        <div className="player-tools" aria-label="Video player tools">
          <button type="button" onClick={() => seekBy(-10)} aria-label="Back 10 seconds">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 8H5V4m.5 3.5A8 8 0 1 1 4 15" /><text x="8.2" y="15.2">10</text></svg>
            <span>Back</span>
          </button>
          <button type="button" onClick={() => seekBy(10)} aria-label="Forward 10 seconds">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 8h4V4m-.5 3.5A8 8 0 1 0 20 15" /><text x="8.2" y="15.2">10</text></svg>
            <span>Forward</span>
          </button>
          <label className="speed-control">
            <span>Speed</span>
            <select
              id="playback-speed"
              name="playback-speed"
              value={playbackRate}
              onChange={(event) => changePlaybackRate(event.target.value)}
              aria-label="Playback speed"
            >
              <option value="0.5">0.5×</option>
              <option value="0.75">0.75×</option>
              <option value="1">1×</option>
              <option value="1.25">1.25×</option>
              <option value="1.5">1.5×</option>
              <option value="2">2×</option>
            </select>
          </label>
          <button type="button" onClick={() => void openPictureInPicture()} aria-label="Mini player (Picture in Picture)">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v14H4zM12 12h6v5h-6z" /></svg>
            <span>Mini player</span>
          </button>
          <button type="button" onClick={() => void openFullscreen()} aria-label="Open fullscreen video">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 3H3v5m13-5h5v5M8 21H3v-5m13 5h5v-5" /></svg>
            <span>Fullscreen</span>
          </button>
        </div>
      )}
    </div>
  )
}

function App() {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.search))
  const [authentication, setAuthentication] = useState<AuthenticationState>({ status: 'checking' })
  const [authDiagnostics, setAuthDiagnostics] = useState<AuthDiagnostic[]>(getAuthDiagnostics)
  const [authRetryKey, setAuthRetryKey] = useState(0)
  const [retryKey, setRetryKey] = useState(0)
  const [progress, setProgress] = useState<ProgressState>(INITIAL_PROGRESS)
  const [video, setVideo] = useState<VideoInfo | null>(null)
  const [error, setError] = useState<AppError | null>(null)
  const [videoDiagnostics, setVideoDiagnostics] = useState<VideoDiagnostic[]>([])
  const [isLoading, setIsLoading] = useState(route.name === 'video')
  // Guards against duplicate /v2/video calls (e.g. StrictMode double-mount); that call is costly.
  const resolvedKeyRef = useRef<string | null>(null)
  const inFlightRef = useRef<AbortController | null>(null)
  const videoDiagnosticSequenceRef = useRef(0)
  const videoDiagnosticWriterRef = useRef<DiagnosticWriter>((step, message, details) => {
    setVideoDiagnostics((entries) => [
      ...entries.slice(-49),
      {
        id: ++videoDiagnosticSequenceRef.current,
        time: new Date().toISOString(),
        step,
        message,
        details,
      },
    ])
  })

  useEffect(() => subscribeAuthDiagnostics(setAuthDiagnostics), [])

  useEffect(() => {
    let active = true
    void authenticateTelegramSession(authRetryKey > 0).then((result) => {
      if (!active) return
      if (result.status === 'authenticated') {
        resolvedKeyRef.current = null
        setVideo(null)
        setError(null)
        setVideoDiagnostics([])
        setProgress(INITIAL_PROGRESS)
        setIsLoading(true)
      }
      setAuthentication(result)
    })
    return () => {
      active = false
    }
  }, [authRetryKey])

  useEffect(() => {
    const handleRouteChange = () => {
      const nextRoute = parseRoute(window.location.search)
      if (nextRoute.name === 'video') {
        setIsLoading(true)
        setVideo(null)
        setError(null)
        setVideoDiagnostics([])
        setProgress(INITIAL_PROGRESS)
      }
      setRoute(nextRoute)
    }
    window.addEventListener('popstate', handleRouteChange)
    return () => window.removeEventListener('popstate', handleRouteChange)
  }, [])

  useEffect(() => {
    if (route.name !== 'video' || authentication.status !== 'authenticated') {
      return
    }

    const requestKey = `${route.url}::${retryKey}`
    // Skip re-firing for a key already dispatched (StrictMode remount) so /v2/video runs once.
    if (resolvedKeyRef.current === requestKey) {
      return
    }
    resolvedKeyRef.current = requestKey

    // Abort a stale in-flight request only when the key actually changed (real route/retry).
    inFlightRef.current?.abort()
    const controller = new AbortController()
    inFlightRef.current = controller

    void resolveVideo(
      route.url,
      controller.signal,
      setProgress,
      videoDiagnosticWriterRef.current,
    )
      .then((result) => {
        if (!controller.signal.aborted) {
          setVideo(result)
          setIsLoading(false)
        }
      })
      .catch((caughtError: unknown) => {
        if (controller.signal.aborted) return
        setError(
          caughtError instanceof AppError
            ? caughtError
            : new AppError('Something unexpected happened. Please try again.'),
        )
        setIsLoading(false)
      })
  }, [authentication.status, route, retryKey])

  const retryAuthentication = () => {
    inFlightRef.current?.abort()
    setAuthentication({ status: 'checking' })
    setAuthRetryKey((key) => key + 1)
  }

  const retry = () => {
    setIsLoading(true)
    setVideo(null)
    setError(null)
    setVideoDiagnostics([])
    setProgress(INITIAL_PROGRESS)
    setRetryKey((key) => key + 1)
  }

  const fileSize = formatFileSize(video?.size)
  const isAuthenticated = authentication.status === 'authenticated'
  const authenticatedUser = authentication.status === 'authenticated' ? authentication.user : null

  return (
    <main className="app-shell">
      <header className="brand" aria-label="DiskWala video downloader">
        <div className="brand-identity">
          <span className="brand-mark"><VideoIcon /></span>
          <span>DiskWala</span>
        </div>
        {isAuthenticated && video && !isLoading && !error && (
          <a
            className="secondary-button download-button"
            href={video.url}
            target="_blank"
            rel="noopener noreferrer"
            download={video.name}
            aria-label="Download video"
          >
            <DownloadIcon />
            <span>Download</span>
          </a>
        )}
      </header>

      <section className="content-card">
        {authentication.status === 'checking' && (
          <div className="loading-state" role="status" aria-live="polite">
            <div className="spinner-wrap" aria-hidden="true">
              <span className="spinner" />
              <span className="progress-number">•••</span>
            </div>
            <p className="eyebrow">Telegram security</p>
            <h1>Verifying your session</h1>
            <p className="supporting-text">Confirming that this Mini App was opened securely from the bot.</p>
          </div>
        )}

        {authentication.status === 'unavailable' && (
          <div className="empty-state error-state" role="alert">
            <span className="state-icon error-icon" aria-hidden="true">!</span>
            <p className="eyebrow">Telegram required</p>
            <h1>Open this inside Telegram</h1>
            <p className="supporting-text">{authentication.message}</p>
          </div>
        )}

        {(authentication.status === 'rejected' || authentication.status === 'error') && (
          <div className="empty-state error-state" role="alert">
            <span className="state-icon error-icon" aria-hidden="true">!</span>
            <p className="eyebrow">Authentication failed</p>
            <h1>Session could not be verified</h1>
            <p className="supporting-text">{authentication.message}</p>
            <button className="primary-button" type="button" onClick={retryAuthentication}>
              Check again
            </button>
            <section className="diagnostic-panel" aria-label="Authentication diagnostics">
              <h2>Authentication diagnostics</h2>
              <p>Safe technical log — initData values and credentials are not shown.</p>
              <div className="diagnostic-log">
                {authDiagnostics.map((entry) => (
                  <article key={entry.id} className="diagnostic-entry">
                    <div>
                      <time>{entry.time.slice(11, 23)}</time>
                      <code>{entry.step}</code>
                    </div>
                    <strong>{entry.message}</strong>
                    {entry.details && <pre>{JSON.stringify(entry.details, null, 2)}</pre>}
                  </article>
                ))}
              </div>
            </section>
          </div>
        )}

        {isAuthenticated && route.name === 'missing' && (
          <div className="empty-state" role="status">
            <span className="state-icon"><VideoIcon /></span>
            <p className="eyebrow">No URL detected</p>
            <h1>Open a DiskWala video link</h1>
            <p className="supporting-text">
              This page needs a secure DiskWala link in the <code>url</code> query parameter.
              Please return to the bot and open the video again.
            </p>
            <div className="route-hint" aria-label="Required URL format">
              <span>Expected route</span>
              <code>?url=https%3A%2F%2Fdiskwala.com%2F…</code>
            </div>
          </div>
        )}

        {isAuthenticated && route.name === 'invalid' && (
          <div className="empty-state error-state" role="alert">
            <span className="state-icon error-icon" aria-hidden="true">!</span>
            <p className="eyebrow">Unsupported link</p>
            <h1>That URL cannot be used</h1>
            <p className="supporting-text">{route.message}</p>
            <p className="recovery-note">Return to the bot and choose a DiskWala video link.</p>
          </div>
        )}

        {isAuthenticated && route.name === 'video' && isLoading && (
          <div className="loading-state" role="status" aria-live="polite">
            {authenticatedUser && (
              <p className="user-greeting">
                Hi {authenticatedUser.first_name}
                {authenticatedUser.username && <span> @{authenticatedUser.username}</span>}
                <span className="user-id"> · ID {authenticatedUser.id}</span>
              </p>
            )}
            <div className="spinner-wrap" aria-hidden="true">
              <span className="spinner" />
              <span className="progress-number">{progress.percent}%</span>
            </div>
            <p className="eyebrow">Processing video</p>
            <h1>{progress.title}</h1>
            <p className="supporting-text">{progress.detail}</p>
            <div
              className="progress-track"
              role="progressbar"
              aria-label="Video preparation progress"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={progress.percent}
            >
              <span style={{ width: `${progress.percent}%` }} />
            </div>
            <ol className="steps" aria-label="Video preparation steps">
              <li className={progress.percent >= 10 ? 'active' : ''}>Check link</li>
              <li className={progress.percent >= 62 ? 'active' : ''}>Fetch details</li>
              <li className={progress.percent >= 84 ? 'active' : ''}>Prepare file</li>
            </ol>
          </div>
        )}

        {isAuthenticated && route.name === 'video' && error && !isLoading && (
          <div className="empty-state error-state" role="alert">
            <span className="state-icon error-icon" aria-hidden="true">!</span>
            <p className="eyebrow">Could not prepare video</p>
            <h1>Something went wrong</h1>
            <p className="supporting-text">{error.message}</p>
            <p className="error-code">Reference: {error.code}</p>
            <button className="primary-button" type="button" onClick={retry}>
              Try again
            </button>
            <section className="diagnostic-panel" aria-label="Video request diagnostics">
              <h2>Video request diagnostics</h2>
              <p>Safe technical log — API keys, authorization values, request bodies, and URL queries are hidden.</p>
              <div className="diagnostic-log">
                {videoDiagnostics.map((entry) => (
                  <article key={entry.id} className="diagnostic-entry">
                    <div>
                      <time>{entry.time.slice(11, 23)}</time>
                      <code>{entry.step}</code>
                    </div>
                    <strong>{entry.message}</strong>
                    {entry.details && <pre>{JSON.stringify(entry.details, null, 2)}</pre>}
                  </article>
                ))}
              </div>
            </section>
          </div>
        )}

        {isAuthenticated && route.name === 'video' && video && !isLoading && !error && (
          <div className="result-state" aria-live="polite">
            <div className="result-heading">
              <div className="video-copy">
                <h1 title={video.name}>{video.name}</h1>
                <div className="metadata" aria-label="Video details">
                  {video.extension && <span>{video.extension.toUpperCase()}</span>}
                  {fileSize && <span>{fileSize}</span>}
                  {video.views !== undefined && <span>{video.views.toLocaleString()} views</span>}
                </div>
              </div>
            </div>

            <VideoPlayer video={video} />
          </div>
        )}
      </section>

      <footer>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 5 6v5c0 4.6 2.8 8.1 7 10 4.2-1.9 7-5.4 7-10V6l-7-3Zm-3 9 2 2 4-4" /></svg>
        {isAuthenticated ? 'Verified Telegram session' : 'Secure Telegram authentication'}
      </footer>
    </main>
  )
}

export default App
