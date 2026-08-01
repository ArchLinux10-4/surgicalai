import type { StreamChunk, ProjectMemory, PromptTemplate, ImpactAnalysis, MultiFileAnalysis, MemoryPreset } from '../types'

const BASE = (import.meta.env.VITE_API_URL ?? '') + '/api'

/** API base (`{VITE_API_URL||''}/api`) — exported so ancillary helpers
 *  (e.g. clientLog) hit the same origin/prefix as every other API call. */
export const API_BASE = BASE

/** Fire-and-forget clientLog bridge for wrappers defined in this file.
 *  lib/clientLog.ts imports API_BASE/getAuthToken from here, so clientLog must
 *  be pulled in lazily (dynamic import) to avoid a circular module dependency.
 *  Never throws, never blocks the caller. */
function _log(event: string, data: Record<string, unknown> = {}): void {
  try {
    void import('../lib/clientLog')
      .then((m) => m.clientLog(event, data))
      .catch(() => { /* silent no-op */ })
  } catch {
    /* never throw from a logging call */
  }
}

/** Read JWT from persisted auth store without importing zustand (avoids circular deps).
 *  Auth is stored under `surgicalai-auth-{username}` (namespaced) or legacy `surgicalai-auth`.
 *  Exported so ancillary helpers authenticate exactly like the main client. */
export function getAuthToken(): string | null {
  try {
    // Search for namespaced key first (surgicalai-auth-{username})
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (key && key.startsWith('surgicalai-auth-') && key !== 'surgicalai-auth') {
        const data = JSON.parse(localStorage.getItem(key) || '')
        if (data?.token) return data.token
      }
    }
    // Fallback: legacy Zustand persist key
    const raw = localStorage.getItem('surgicalai-auth')
    if (!raw) return null
    const parsed = JSON.parse(raw)
    return parsed?.state?.token ?? parsed?.token ?? null
  } catch {
    return null
  }
}

function authHeaders(): Record<string, string> {
  const token = getAuthToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

/** Build the ws://|wss:// URL for a streaming endpoint.
 *  Mirrors BASE (`{VITE_API_URL||origin}/api`) but swaps the scheme and
 *  carries the JWT as `?token=` because browsers cannot attach Authorization
 *  headers to a WebSocket handshake (and the HTTP auth middleware does not run
 *  for WS scopes — the backend re-auths from this same query param). */
function wsUrl(path: string): string {
  const apiRoot = (import.meta.env.VITE_API_URL ?? '') || window.location.origin
  const wsRoot = apiRoot.replace(/^http/i, 'ws') // http→ws, https→wss
  const token = getAuthToken() ?? ''
  return `${wsRoot}/api${path}?token=${encodeURIComponent(token)}`
}

/**
 * Stream a request over WebSocket, feeding each `data: ` line to `processLine`
 * exactly as the SSE path does (identical framing — the backend forwards the
 * same StreamingResponse chunks verbatim).
 *
 * WHY WS: Railway caps HTTP requests at 15 min and closes idle ones; WS is
 * exempt from both limits, so long agent runs no longer hit a transport wall.
 *
 * SAFETY — fall back to HTTP (`onOpenFail`) ONLY when the socket never opens.
 * Once the socket is open the server may have already started work; re-running
 * over HTTP would double-apply edits.  A mid-stream drop therefore does NOT
 * fall back — it just ends the stream, and the backend safety-net persists any
 * partial work.  Abort (session switch) mirrors the fetch path: no onDone.
 */
function streamViaWS(
  path: string,
  data: unknown,
  controller: AbortController,
  processLine: (line: string) => void,
  fireDone: () => void,
  onOpenFail: () => void,
  registerSender?: (send: ((msg: unknown) => void) | null) => void,
): void {
  let ws: WebSocket
  try {
    ws = new WebSocket(wsUrl(path))
  } catch {
    onOpenFail()
    return
  }
  let opened = false
  let aborted = false
  let buffer = ''

  const closeWs = () => { aborted = true; try { ws.close() } catch { /* noop */ } }
  controller.signal.addEventListener('abort', closeWs)

  ws.onopen = () => {
    opened = true
    ws.send(JSON.stringify(data))
    // Expose a back-channel sender (human-in-the-loop file responses). Only
    // the WS transport is bidirectional, so this is the sole path that can
    // answer a `file_needed` prompt.
    registerSender?.((msg: unknown) => {
      try { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg)) } catch { /* noop */ }
    })
  }

  ws.onmessage = (ev: MessageEvent) => {
    buffer += typeof ev.data === 'string' ? ev.data : ''
    const parts = buffer.split('\n')
    buffer = parts.pop() ?? ''
    for (const line of parts) processLine(line.trimEnd())
  }

  // onerror always precedes onclose; do all decisions in onclose (fires once).
  ws.onerror = () => { /* handled in onclose */ }

  ws.onclose = () => {
    controller.signal.removeEventListener('abort', closeWs)
    registerSender?.(null)                  // back-channel is gone
    if (!opened) { onOpenFail(); return }   // never connected → safe to fall back
    if (aborted) return                     // user cancelled → match fetch: no onDone
    if (buffer.trim()) processLine(buffer.trimEnd())
    fireDone()                              // idempotent — no-op if `done` already fired
  }
}

/** Error that carries the HTTP status through to callers.
 *
 *  `request()` used to throw a bare `Error(detail)`, so the status code was
 *  destroyed at the throw site. Callers written as `e?.response?.status` were
 *  therefore ALWAYS reading `undefined`, which is why a burst of uploads that
 *  the server rejected with 429 could not be distinguished from any other
 *  failure — and so could never be retried. */
export class ApiError extends Error {
  status: number
  /** Seconds the server asked us to wait (429 responses carry both a
   *  `Retry-After` header and a `retry_after` body field — see
   *  backend/middleware/rate_limiter.py). */
  retryAfter?: number
  constructor(message: string, status: number, retryAfter?: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.retryAfter = retryAfter
  }
}

/** Guard against building a URL from an empty file id.
 *
 *  `/chat/{sid}/files/${fileId}` with an empty id collapses to
 *  `/chat/{sid}/files/`, which the server used to answer with a 307 redirect
 *  to the *list* route. The caller then received a JSON array where it
 *  expected a file, and the apply silently did nothing (proven in session
 *  d021ff07). Fail fast and loudly instead. */
function requireFileId(fileId: string, op: string): string {
  if (!fileId) {
    throw new ApiError(
      `Cannot ${op}: this file has no session id yet. Re-upload the file and try again.`,
      400,
    )
  }
  return fileId
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...authHeaders(), ...options?.headers },
    ...options,
  })
  if (!res.ok) {
    // Auto-logout if the server says our token is invalid/expired.
    // Only redirect if we actually had a token — prevents infinite loop on login page.
    if (res.status === 401) {
      const hadToken = !!getAuthToken()
      try { localStorage.removeItem('surgicalai-auth') } catch {}
      if (hadToken) {
        // Hard navigate to root — authStore will see no token and show login.
        // Use replace so back-button doesn't loop.
        window.location.replace('/')
      }
    }
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    const hdr = Number(res.headers.get('Retry-After'))
    const retryAfter = Number(err?.retry_after) || (Number.isFinite(hdr) && hdr > 0 ? hdr : undefined)
    throw new ApiError(err.detail || `HTTP ${res.status}`, res.status, retryAfter)
  }
  return res.json()
}

/** Axios-like client for use in auth flows (open endpoints that don't need token). */
export const apiClient = {
  get: async (path: string) => {
    const url = (import.meta.env.VITE_API_URL ?? '') + path
    const res = await fetch(url, { headers: { 'Content-Type': 'application/json' } })
    const data = await res.json()
    if (!res.ok) throw { response: { data } }
    return { data }
  },
  post: async (path: string, body: any) => {
    const url = (import.meta.env.VITE_API_URL ?? '') + path
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    const data = await res.json()
    if (!res.ok) throw { response: { data } }
    return { data }
  },
}

// Settings
export const api = {
  settings: {
    get: () => request<any>('/settings'),
    update: (data: any) => request('/settings', { method: 'POST', body: JSON.stringify(data) }),
    verifyKey: (key: string) => request('/settings/verify-key', { method: 'POST', body: JSON.stringify({ key }) }),
    verifyAnthropicKey: (key: string) => request('/settings/verify-anthropic-key', { method: 'POST', body: JSON.stringify({ key }) }),
    getModels: () => request<any>('/settings/models'),
    geminiStatus: () => request<any>('/settings/gemini-status'),
    verifyGeminiKey: (key: string) => request('/settings/verify-gemini-key', { method: 'POST', body: JSON.stringify({ key }) }),
    // xAI Grok — mirrors the Gemini/Anthropic wrappers above. Each new wrapper
    // emits a clientLog event so the browser side of the Grok flow lands in the
    // downloadable debug log, exactly like the upload paths in ChatPanel.tsx.
    // NOTE: clientLog is loaded via a lazy dynamic import, not a top-level
    // one — lib/clientLog.ts imports API_BASE/getAuthToken from THIS file, so a
    // static import here would create a circular module dependency (this file's
    // own header comment calls out avoiding exactly that). _log() never throws
    // and never blocks the request.
    grokStatus: () => { _log('grok_status_fetch'); return request<any>('/settings/grok-status') },
    verifyGrokKey: (key: string) => {
      _log('grok_key_verify_submitted', { keyLength: key.length })
      return request('/settings/verify-grok-key', { method: 'POST', body: JSON.stringify({ key }) })
    },
    browseDirectory: () => request<any>('/settings/browse-directory', { method: 'POST' }),
  },
  chat: {
    createSession: (data: any) => request<any>('/chat/sessions', { method: 'POST', body: JSON.stringify(data) }),
    getSessions: () => request<any[]>('/chat/sessions'),
    getMessages: (sessionId: string) => request<any[]>(`/chat/sessions/${sessionId}/messages`),
    send: (data: any) => request<any>('/chat/send', { method: 'POST', body: JSON.stringify(data) }),
    deleteSession: (id: string) => request(`/chat/sessions/${id}`, { method: 'DELETE' }),
    renameSession: (id: string, title: string) => request(`/chat/sessions/${id}`, { method: 'PATCH', body: JSON.stringify({ title }) }),
    search: (q: string) => request<any[]>(`/chat/search?q=${encodeURIComponent(q)}`),
  },
  files: {
    getTree: (root?: string) => request<any>(`/files/tree${root ? `?root=${encodeURIComponent(root)}` : ''}`),
    read: (path: string) => request<any>(`/files/read?path=${encodeURIComponent(path)}`),
    save: (path: string, content: string) => request('/files/save', { method: 'POST', body: JSON.stringify({ path, content }) }),
    getSymbols: (path: string) => request<any>(`/files/symbols?path=${encodeURIComponent(path)}`),
    listBackups: (path: string) => request<any[]>(`/files/backups?path=${encodeURIComponent(path)}`),
    restore: (file_path: string, backup_path: string) => request('/files/restore', { method: 'POST', body: JSON.stringify({ file_path, backup_path }) }),
  },
  surgical: {
    analyze: (data: any) => request<any>('/surgical/analyze', { method: 'POST', body: JSON.stringify(data) }),
    apply: (data: any) => request<any>('/surgical/apply', { method: 'POST', body: JSON.stringify(data) }),
    applyAll: (data: any) => request<any>('/surgical/apply-all', { method: 'POST', body: JSON.stringify(data) }),
    getHistory: (filePath?: string) => request<any[]>(`/surgical/history${filePath ? `?file_path=${encodeURIComponent(filePath)}` : ''}`),
    markApplied: (sessionId: string, changeId: string) =>
      request<any>(`/surgical/applied/${sessionId}/${encodeURIComponent(changeId)}`, { method: 'POST' }),
    unmarkApplied: (sessionId: string, changeId: string) =>
      request<any>(`/surgical/applied/${sessionId}/${encodeURIComponent(changeId)}`, { method: 'DELETE' }),
    getApplied: (sessionId: string) =>
      request<{ applied_ids: string[] }>(`/surgical/applied/${sessionId}`),
  },
  git: {
    status: (repoPath: string) => request<any>(`/git/status?repo_path=${encodeURIComponent(repoPath)}`),
    diff: (repoPath: string, filePath?: string) => request<any>(`/git/diff?repo_path=${encodeURIComponent(repoPath)}${filePath ? `&file_path=${encodeURIComponent(filePath)}` : ''}`),
    commit: (data: any) => request('/git/commit', { method: 'POST', body: JSON.stringify(data) }),
    log: (repoPath: string) => request<any[]>(`/git/log?repo_path=${encodeURIComponent(repoPath)}`),
  },

  stream: {
    chat: (data: any, onChunk: (chunk: StreamChunk) => void, onDone: (fullText: string) => void, onError: (err: string) => void): AbortController => {
      const controller = new AbortController()
      const fullText: string[] = []
      let doneCalled = false
      const fireDone = () => { if (!doneCalled) { doneCalled = true; onDone(fullText.join('')) } }

      fetch(`${BASE}/chat/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(data),
        signal: controller.signal,
      }).then(async res => {
        if (!res.ok) {
          const err = await res.json().catch(() => ({ detail: res.statusText }))
          onError(err.detail || `HTTP ${res.status}`)
          fireDone()
          return
        }
        const reader = res.body!.getReader()
        const decoder = new TextDecoder()

        const pump = () => reader.read().then(({ done, value }) => {
          if (done) { fireDone(); return }
          const text = decoder.decode(value, { stream: true })
          const lines = text.split('\n')
          for (const line of lines) {
            if (line.startsWith('data: ')) {
              try {
                const chunk: StreamChunk = JSON.parse(line.slice(6))
                if (chunk.type === 'token') { fullText.push(chunk.content); onChunk(chunk) }
                else if (chunk.type === 'done') { fireDone() }
                else if (chunk.type === 'error') { onError(chunk.content) }
                else { onChunk(chunk) }
              } catch {}
            }
          }
          pump()
        }).catch(e => { if (e.name !== 'AbortError') { onError(e.message); fireDone() } })

        pump()
      }).catch(e => { if (e.name !== 'AbortError') { onError(e.message); fireDone() } })

      return controller
    },

    smart: (
      data: { session_id: string; message: string; file_ids?: string[]; mode?: 'edit' | 'ask' | 'plan' | 'agent'; force_tasks?: boolean },
      onProgress: (msg: string) => void,
      onToken: (token: string) => void,
      onResult: (result: any) => void,
      onDone: (fullText: string, model?: string) => void,
      onError: (err: string) => void,
      onThinking?: (text: string, phase: 'start' | 'delta' | 'end') => void,
      onCompacting?: (phase: 'start' | 'done', info?: { summary?: string; compacted_count?: number }) => void,
      onEditStart?: () => void,
      onEditEnd?: () => void,
      onTask?: (event: any) => void,
      // Human-in-the-loop: the agent paused because it needs a file that isn't
      // in the session. `respond` sends the file (or a skip) back over the same
      // WebSocket. `onFileCleared` fires when the prompt should be dismissed
      // (provided / skipped / timed out).
      // `respond` returns false when the back-channel is dead (WS already
      // closed) so the caller can surface that instead of hanging forever
      // on a request that will never be delivered.
      onFileNeeded?: (
        info: { filename: string; message: string; retry?: boolean },
        respond: (resp: { filename?: string; content?: string; action?: 'skip' }) => boolean,
      ) => void,
      onFileCleared?: (filename: string) => void,
    ): AbortController => {
      const controller = new AbortController()
      const tokens: string[] = []
      let doneCalled = false
      let _modelUsed = ''
      // Back-channel sender, populated once the WS opens (null on HTTP/SSE).
      let sendToServer: ((msg: unknown) => void) | null = null
      const fireDone = () => { if (!doneCalled) { doneCalled = true; onDone(tokens.join(''), _modelUsed) } }

      // Transport-independent SSE line handler — shared by WS and fetch paths.
      const processLine = (line: string) => {
        if (!line.startsWith('data: ')) return
        try {
          const chunk = JSON.parse(line.slice(6))
          if (chunk.type === 'progress') onProgress(chunk.content)
          else if (chunk.type === 'token') { tokens.push(chunk.content); onToken(chunk.content) }
          else if (chunk.type === 'smart_result') {
            // Natural pipeline: result may include natural_text already streamed as tokens
            const result = JSON.parse(chunk.content)
            if (chunk.model) { _modelUsed = chunk.model; result._model = chunk.model }
            onResult(result)
          }
          else if (chunk.type === 'chat') { tokens.push(chunk.content); onToken(chunk.content) }
          else if (chunk.type === 'done') { if (chunk.model) _modelUsed = chunk.model; fireDone() }
          else if (chunk.type === 'error') onError(chunk.content)
          else if (chunk.type === 'thinking_start') onThinking?.('', 'start')
          else if (chunk.type === 'thinking') onThinking?.(chunk.content, 'delta')
          else if (chunk.type === 'thinking_end') onThinking?.('', 'end')
          else if (chunk.type === 'compacting') onCompacting?.('start')
          else if (chunk.type === 'compacting_done') onCompacting?.('done', { summary: chunk.summary, compacted_count: chunk.compacted_count })
          else if (chunk.type === 'edit_start') onEditStart?.()
          else if (chunk.type === 'edit_end') onEditEnd?.()
          else if (chunk.type === 'file_needed') {
            onFileNeeded?.(
              { filename: chunk.filename, message: chunk.content, retry: chunk.retry },
              (resp) => {
                if (!sendToServer) return false   // back-channel is gone — never silently no-op
                sendToServer({ type: 'file_response', ...resp })
                return true
              },
            )
          }
          else if (chunk.type === 'file_needed_cleared') onFileCleared?.(chunk.filename)
          else if (
            chunk.type === 'planning_started' ||
            chunk.type === 'task_plan' || chunk.type === 'task_start' ||
            chunk.type === 'task_progress' || chunk.type === 'task_done' ||
            chunk.type === 'task_thinking' ||
            chunk.type === 'task_blocked' || chunk.type === 'task_cancelled' ||
            chunk.type === 'tasks_complete'
          ) onTask?.(chunk)
        } catch {}
      }

      // HTTP/SSE transport (also the fallback when the WS never opens).
      const runViaFetch = () => {
        fetch(`${BASE}/chat/smart-stream`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...authHeaders() },
          body: JSON.stringify(data),
          signal: controller.signal,
        }).then(async res => {
          if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: res.statusText }))
            onError(err.detail || `HTTP ${res.status}`)
            fireDone()
            return
          }
          const reader = res.body!.getReader()
          const decoder = new TextDecoder()
          let lineBuffer = ''

          const pump = () => reader.read().then(({ done, value }) => {
            if (done) { fireDone(); return }
            lineBuffer += decoder.decode(value, { stream: true })
            const parts = lineBuffer.split('\n')
            lineBuffer = parts.pop() ?? ''
            for (const line of parts) {
              processLine(line.trimEnd())
            }
            pump()
          }).catch(e => { if (e.name !== 'AbortError') { onError(e.message); fireDone() } })

          pump()
        }).catch(e => { if (e.name !== 'AbortError') { onError(e.message); fireDone() } })
      }

      // WebSocket-first (removes Railway's 15-min transport wall); on a
      // connect-time failure only, transparently fall back to HTTP/SSE.
      if (typeof WebSocket !== 'undefined') {
        streamViaWS('/chat/ws/smart-stream', data, controller, processLine, fireDone, runViaFetch,
          (send) => { sendToServer = send })
      } else {
        runViaFetch()
      }

      return controller
    },

    // v1.4: execute one planned task in its own short-lived SSE stream.
    // The client calls this once per task, in sequence, after /smart-stream
    // returns the plan. Routes task lifecycle events to the same UI handlers.
    executeTask: (
      data: { session_id: string; run_id: string; task_id: string },
      onProgress: (msg: string) => void,
      onResult: (result: any) => void,
      onDone: () => void,
      onError: (err: string) => void,
      onTask: (event: any) => void,
    ): AbortController => {
      const controller = new AbortController()
      let doneCalled = false
      const fireDone = () => { if (!doneCalled) { doneCalled = true; onDone() } }

      // Transport-independent SSE line handler — shared by WS and fetch paths.
      const processLine = (line: string) => {
        if (!line.startsWith('data: ')) return
        try {
          const chunk = JSON.parse(line.slice(6))
          if (chunk.type === 'smart_result') onResult(JSON.parse(chunk.content))
          else if (chunk.type === 'done') fireDone()
          else if (chunk.type === 'error') onError(chunk.content)
          else if (
            chunk.type === 'task_start' || chunk.type === 'task_progress' ||
            chunk.type === 'task_thinking' ||
            chunk.type === 'task_done' || chunk.type === 'task_blocked' ||
            chunk.type === 'task_cancelled' || chunk.type === 'tasks_complete'
          ) {
            if (chunk.type === 'task_progress') onProgress(chunk.content)
            onTask(chunk)
          }
        } catch {}
      }

      // HTTP/SSE transport (also the fallback when the WS never opens).
      const runViaFetch = () => {
        fetch(`${BASE}/chat/execute-task`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...authHeaders() },
          body: JSON.stringify(data),
          signal: controller.signal,
        }).then(async res => {
          if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: res.statusText }))
            onError(err.detail || `HTTP ${res.status}`)
            fireDone()
            return
          }
          const reader = res.body!.getReader()
          const decoder = new TextDecoder()
          let lineBuffer = ''

          const pump = () => reader.read().then(({ done, value }) => {
            if (done) { fireDone(); return }
            lineBuffer += decoder.decode(value, { stream: true })
            const parts = lineBuffer.split('\n')
            lineBuffer = parts.pop() ?? ''
            for (const line of parts) processLine(line.trimEnd())
            pump()
          }).catch(e => { if (e.name !== 'AbortError') { onError(e.message); fireDone() } })

          pump()
        }).catch(e => { if (e.name !== 'AbortError') { onError(e.message); fireDone() } })
      }

      // WebSocket-first (removes Railway's 15-min transport wall); on a
      // connect-time failure only, transparently fall back to HTTP/SSE.
      if (typeof WebSocket !== 'undefined') {
        streamViaWS('/chat/ws/execute-task', data, controller, processLine, fireDone, runViaFetch)
      } else {
        runViaFetch()
      }

      return controller
    },

    surgical: (data: any, onProgress: (msg: string) => void, onResult: (result: any) => void, onError: (err: string) => void): AbortController => {
      const controller = new AbortController()

      fetch(`${BASE}/surgical/analyze-stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(data),
        signal: controller.signal,
      }).then(async res => {
        if (!res.ok) {
          const err = await res.json().catch(() => ({ detail: res.statusText }))
          onError(err.detail || `HTTP ${res.status}`)
          return
        }
        const reader = res.body!.getReader()
        const decoder = new TextDecoder()
        let lineBuffer = ''

        const processLine = (line: string) => {
          if (!line.startsWith('data: ')) return
          try {
            const chunk: StreamChunk = JSON.parse(line.slice(6))
            if (chunk.type === 'progress') { onProgress(chunk.content) }
            else if (chunk.type === 'result') { onResult(JSON.parse(chunk.content)) }
            else if (chunk.type === 'error') { onError(chunk.content) }
          } catch {}
        }

        const pump = () => reader.read().then(({ done, value }) => {
          if (done) return
          lineBuffer += decoder.decode(value, { stream: true })
          const parts = lineBuffer.split('\n')
          lineBuffer = parts.pop() ?? ''
          for (const line of parts) {
            processLine(line.trimEnd())
          }
          pump()
        }).catch(e => { if (e.name !== 'AbortError') onError(e.message) })

        pump()
      }).catch(e => { if (e.name !== 'AbortError') onError(e.message) })

      return controller
    },
  },

  sessionFiles: {
    upload: (sessionId: string, data: { filename: string; content: string; language?: string; origin?: 'uploaded' | 'created' }) =>
      request<any>(`/chat/${sessionId}/files`, { method: 'POST', body: JSON.stringify(data) }),
    /** Multipart upload — sends raw bytes, server converts HEIC→JPEG.
     *  Works on iOS Chrome / WKWebView where base64/canvas paths fail. */
    uploadMultipart: (sessionId: string, formData: FormData): Promise<any> => {
      const token = (() => {
        try {
          for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i)
            if (k && k.startsWith('surgicalai-auth-') && k !== 'surgicalai-auth') {
              const d = JSON.parse(localStorage.getItem(k) || '')
              if (d?.token) return d.token
            }
          }
          const raw = localStorage.getItem('surgicalai-auth')
          if (!raw) return null
          const parsed = JSON.parse(raw)
          return parsed?.state?.token ?? parsed?.token ?? null
        } catch { return null }
      })()
      const headers: Record<string, string> = {}
      if (token) headers['Authorization'] = `Bearer ${token}`
      // Do NOT set Content-Type — browser must set it with the multipart boundary
      return fetch(`${BASE}/chat/${sessionId}/files/upload`, {
        method: 'POST',
        headers,
        body: formData,
      }).then(async res => {
        if (!res.ok) {
          const err = await res.json().catch(() => ({ detail: res.statusText }))
          const hdr = Number(res.headers.get('Retry-After'))
          const retryAfter = Number(err?.retry_after) || (Number.isFinite(hdr) && hdr > 0 ? hdr : undefined)
          throw new ApiError(err.detail || `HTTP ${res.status}`, res.status, retryAfter)
        }
        return res.json()
      })
    },
    /** Bulk-import every code file from a local folder into this session
     *  (defaults to the configured workspace path when no path is given).
     *  Same sandboxing/ignore-lists/size limits as single-file upload;
     *  never overwrites files already edited in this session. */
    importFolder: (sessionId: string, path?: string) =>
      request<{
        imported_count: number
        imported: string[]
        skipped_edited: string[]
        skipped_too_large: string[]
        skipped_session_cap: string[]
        failed: { filename: string; reason: string }[]
        truncated: boolean
        folder: string
      }>(`/chat/${sessionId}/files/import-folder`, {
        method: 'POST',
        body: JSON.stringify(path ? { path } : {}),
      }),
    list: (sessionId: string) =>
      request<any[]>(`/chat/${sessionId}/files`),
    get: (sessionId: string, fileId: string) =>
      request<any>(`/chat/${sessionId}/files/${requireFileId(fileId, 'load this file')}`),
    /** Resolve the full import graph (components + CSS + npm deps) for a live
     *  preview. Pass `content` to preview an unsaved/modified version. */
    previewBundle: (sessionId: string, fileId: string, content?: string) =>
      request<any>(`/chat/${sessionId}/files/${requireFileId(fileId, 'preview this file')}/preview-bundle`, {
        method: 'POST',
        body: JSON.stringify(content != null ? { content } : {}),
      }),
    update: (sessionId: string, fileId: string, content: string, label?: string) =>
      request<any>(`/chat/${sessionId}/files/${requireFileId(fileId, 'save this file')}`, { method: 'PUT', body: JSON.stringify(label ? { content, label } : { content }) }),
    undo: (sessionId: string, fileId: string) =>
      request<any>(`/chat/${sessionId}/files/${requireFileId(fileId, 'undo this file')}/undo`, { method: 'POST' }),
    /** Full, browsable edit history for a file — every past saved state, not just the last one. */
    listVersions: (sessionId: string, fileId: string) =>
      request<{ id: string; lines: number; symbol_count: number; label: string; created_at: string }[]>(
        `/chat/${sessionId}/files/${requireFileId(fileId, 'list versions')}/versions`
      ),
    restoreVersion: (sessionId: string, fileId: string, versionId: string) =>
      request<any>(`/chat/${sessionId}/files/${requireFileId(fileId, 'restore this version')}/versions/${versionId}/restore`, { method: 'POST' }),
    delete: (sessionId: string, fileId: string) =>
      request(`/chat/${sessionId}/files/${requireFileId(fileId, 'delete this file')}`, { method: 'DELETE' }),
    download: async (sessionId: string, fileId: string, filename: string) => {
      const file = await request<any>(`/chat/${sessionId}/files/${fileId}`)
      const blob = new Blob([file.content ?? ''], { type: 'text/plain' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      a.click()
      URL.revokeObjectURL(url)
    },
  },

  auth: {
    getUsers: () => request<any[]>('/auth/users'),
    createUser: (data: any) => request<any>('/auth/users', { method: 'POST', body: JSON.stringify(data) }),
    deleteUser: (id: string) => request<any>(`/auth/users/${id}`, { method: 'DELETE' }),
    getPresence: () => request<any[]>('/auth/presence'),
    changePassword: (currentPassword: string, newPassword: string, confirmPassword: string) =>
      request<any>('/auth/change-password', {
        method: 'POST',
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
          confirm_password: confirmPassword,
        }),
      }),
  },

  // Element Picker — local-install only (server hides these behind is_hosted;
  // see routers/element_picker.py). Attaches to the user's own already-running
  // Chrome via CDP (playwright connectOverCDP), never launches/downloads one.
  elementPicker: {
    status: () => request<{ available: boolean; connected: boolean; cdp_url?: string | null; page_url?: string | null }>('/element-picker/status'),
    /** One-click launch: starts a dedicated picker-profile Chrome window with
     *  the debug port open (no-ops if one is already listening). Never
     *  touches the user's everyday Chrome window/profile. */
    launch: (cdpUrl: string) =>
      request<{ launched: boolean; already_running: boolean }>('/element-picker/launch', {
        method: 'POST',
        body: JSON.stringify({ cdp_url: cdpUrl }),
      }),
    connect: (cdpUrl: string) =>
      request<{ connected: boolean; cdp_url: string | null; page_url: string | null }>('/element-picker/connect', {
        method: 'POST',
        body: JSON.stringify({ cdp_url: cdpUrl }),
      }),
    screenshot: () => request<{ image_base64: string; mime_type: string }>('/element-picker/screenshot'),
    pick: (x: number, y: number) =>
      request<{ tag: string; id: string | null; className: string | null; text: string; outerHTML: string; rect: { x: number; y: number; width: number; height: number } }>(
        '/element-picker/pick',
        { method: 'POST', body: JSON.stringify({ x, y }) }
      ),
    disconnect: () => request<{ connected: boolean }>('/element-picker/disconnect', { method: 'POST' }),
    /** hard=true bypasses the browser cache (Ctrl/Cmd+Shift+R equivalent) —
     *  the fix for frontend dev loops where a plain reload keeps showing
     *  stale JS/CSS served with cache-friendly headers. */
    reload: (hard: boolean) =>
      request<{ connected: boolean; cdp_url: string | null; page_url: string | null }>('/element-picker/reload', {
        method: 'POST',
        body: JSON.stringify({ hard }),
      }),
    /** Live hover-highlight (official CDP `Overlay.setInspectMode` — the
     *  same mechanism Chrome DevTools' own "inspect element" uses). Chrome
     *  paints the highlight itself, so it shows up for free in the
     *  existing screencast frames — no extra rendering here. Must only be
     *  enabled while Pick mode is active (see ElementPickerPanel.tsx). */
    setInspectMode: (enabled: boolean) =>
      request<{ connected: boolean }>('/element-picker/inspect-mode', {
        method: 'POST',
        body: JSON.stringify({ enabled }),
      }),
    /** WS URL for the live-view screencast (see routers/element_picker.py
     *  ws/stream). Carries the JWT as `?token=` — same convention as the
     *  chat WS transport, since the HTTP auth middleware doesn't run for
     *  WebSocket scopes. `w`/`h`/`dpr` tell the backend the panel's real
     *  pixel size so the screencast is captured sharp on HiDPI screens
     *  instead of a fixed low-res frame stretched to fill a bigger box. */
    wsStreamUrl: (opts?: { w?: number; h?: number; dpr?: number }) => {
      const base = wsUrl('/element-picker/ws/stream')
      if (!opts) return base
      const qp = new URLSearchParams()
      if (opts.w) qp.set('w', String(Math.round(opts.w)))
      if (opts.h) qp.set('h', String(Math.round(opts.h)))
      if (opts.dpr) qp.set('dpr', String(opts.dpr))
      const extra = qp.toString()
      return extra ? `${base}&${extra}` : base
    },
  },

  context: {
    getMemory: (workspacePath: string) => request<ProjectMemory>(`/context/memory?workspace_path=${encodeURIComponent(workspacePath)}`),
    saveMemory: (data: any) => request<any>('/context/memory', { method: 'POST', body: JSON.stringify(data) }),
    getGlobalMemory: () => request<ProjectMemory>('/context/memory/global'),
    saveGlobalMemory: (content: string) => request<any>('/context/memory/global', { method: 'POST', body: JSON.stringify({ content }) }),
    getMemoryPresets: () => request<MemoryPreset[]>('/context/memory/presets'),
    getTemplates: () => request<PromptTemplate[]>('/context/templates'),
    createTemplate: (data: any) => request<any>('/context/templates', { method: 'POST', body: JSON.stringify(data) }),
    deleteTemplate: (id: string) => request<any>(`/context/templates/${id}`, { method: 'DELETE' }),
    getImpact: (symbolPath: string, filePath: string, workspacePath?: string) => request<ImpactAnalysis>(`/context/impact?symbol_path=${encodeURIComponent(symbolPath)}&file_path=${encodeURIComponent(filePath)}${workspacePath ? `&workspace_path=${encodeURIComponent(workspacePath)}` : ''}`),
    multiAnalyze: (data: any) => request<MultiFileAnalysis>('/context/multi-analyze', { method: 'POST', body: JSON.stringify(data) }),
  },

  linear: {
    status: () => request<any>('/linear/status'),
    connect: (token: string) => request<any>('/linear/connect', { method: 'POST', body: JSON.stringify({ token }) }),
    disconnect: () => request<any>('/linear/disconnect', { method: 'DELETE' }),
    teams: () => request<any>('/linear/teams'),
    issues: (params?: { team_id?: string; state?: string; limit?: number }) => {
      const q = new URLSearchParams()
      if (params?.team_id) q.set('team_id', params.team_id)
      if (params?.state) q.set('state', params.state)
      if (params?.limit) q.set('limit', String(params.limit))
      return request<any>(`/linear/issues?${q}`)
    },
    issue: (id: string) => request<any>(`/linear/issues/${id}`),
    complete: (id: string, comment?: string) => request<any>(`/linear/issues/${id}/complete`, { method: 'POST', body: JSON.stringify({ comment }) }),
  },
  deploy: {
    status: () => request<any>('/deploy/status'),
    poll: () => request<any>('/deploy/poll'),
  },
  tests: {
    detect: (sessionId: string) => request<any>(`/tests/detect/${sessionId}`),
    run: (sessionId: string, fileId?: string) => request<any>('/tests/run', { method: 'POST', body: JSON.stringify({ session_id: sessionId, file_id: fileId }) }),
    status: (runId: string) => request<any>(`/tests/status/${runId}`),
  },

  // v2.0: server-side task runner. start() hands run execution to the
  // backend supervisor; a non-ok response means the feature is disabled or
  // unavailable and the caller falls back to the browser-driven queue.
  runs: {
    start: (sessionId: string, runId: string) =>
      request<{ ok: boolean; mode: string; total?: number; pending?: number }>(
        '/runs/start', { method: 'POST', body: JSON.stringify({ session_id: sessionId, run_id: runId }) }),
    status: (sessionId: string, runId: string) =>
      request<{ active: boolean; enabled?: boolean; wave?: number; pending?: number; total?: number }>(
        `/runs/status?run_id=${encodeURIComponent(runId)}&session_id=${encodeURIComponent(sessionId)}`),
  },

  tasks: {
    list: (sessionId: string, runId?: string) =>
      request<import('../types').AgentTask[]>(`/tasks?session_id=${encodeURIComponent(sessionId)}${runId ? `&run_id=${encodeURIComponent(runId)}` : ''}`),
    cancel: (taskId: string) => request<any>(`/tasks/${taskId}/cancel`, { method: 'POST', body: JSON.stringify({}) }),
    cancelAll: (sessionId: string, runId?: string) =>
      request<any>('/tasks/cancel-all', { method: 'POST', body: JSON.stringify({ session_id: sessionId, run_id: runId }) }),
  },

  datalab: {
    /** Feature flag — UI hides all spreadsheet affordances when false. */
    enabled: () => request<{ enabled: boolean }>('/datalab/enabled'),
    /** Run a natural-language transform. Resolves to {ok, file?, qa, sql?, attempts, trail}. */
    transform: (sessionId: string, fileId: string, prompt: string) =>
      request<any>(`/datalab/${sessionId}/transform`, {
        method: 'POST',
        body: JSON.stringify({ file_id: fileId, prompt }),
      }),
    versions: (sessionId: string, fileId: string) =>
      request<any>(`/datalab/${sessionId}/versions/${fileId}`),
    /** Download the real binary (xlsx/csv) for a spreadsheet file row. */
    download: async (sessionId: string, fileId: string, filename: string) => {
      const res = await fetch(`${BASE}/datalab/${sessionId}/download/${fileId}`, {
        headers: { ...authHeaders() },
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }))
        throw new Error(err.detail || `HTTP ${res.status}`)
      }
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      a.click()
      URL.revokeObjectURL(url)
    },
  },

  github: {
    status: () => request<any>('/github/status'),
    connect: (pat: string) => request<any>('/github/connect', { method: 'POST', body: JSON.stringify({ pat }) }),
    disconnect: () => request<any>('/github/disconnect', { method: 'DELETE' }),
    repos: () => request<any>('/github/repos'),
    branches: (owner: string, repo: string) => request<any>(`/github/repos/${owner}/${repo}/branches`),
    tree: (owner: string, repo: string, branch: string, path = '') =>
      request<any>(`/github/repos/${owner}/${repo}/tree?branch=${encodeURIComponent(branch)}&path=${encodeURIComponent(path)}`),
    load: (body: any) => request<any>('/github/load', { method: 'POST', body: JSON.stringify(body) }),
    commit: (body: any) => request<any>('/github/commit', { method: 'POST', body: JSON.stringify(body) }),
  },

  githubApp: {
    config: () => request<any>('/github-app/config'),
    installUrl: () => request<any>('/github-app/install-url'),
    status: () => request<any>('/github-app/status'),
    setTier: (installation_id: string, tier: string) =>
      request<any>('/github-app/permission-tier', { method: 'POST', body: JSON.stringify({ installation_id, tier }) }),
    disconnect: (installation_id: string) => request<any>(`/github-app/${installation_id}`, { method: 'DELETE' }),
    repos: () => request<any>('/github-app/repos'),
    branches: (owner: string, repo: string) => request<any>(`/github-app/repos/${owner}/${repo}/branches`),
    tree: (owner: string, repo: string, branch: string, path: string) =>
      request<any>(`/github-app/repos/${owner}/${repo}/tree?branch=${encodeURIComponent(branch)}&path=${encodeURIComponent(path)}`),
    load: (body: any) => request<any>('/github-app/load', { method: 'POST', body: JSON.stringify(body) }),
    commit: (body: any) => request<any>('/github-app/commit', { method: 'POST', body: JSON.stringify(body) }),
  },

  vercel: {
    status: () => request<any>('/vercel/status'),
    connect: (token: string) => request<any>('/vercel/connect', { method: 'POST', body: JSON.stringify({ token }) }),
    disconnect: () => request<any>('/vercel/disconnect', { method: 'DELETE' }),
    projects: () => request<any>('/vercel/projects'),
    deployments: (projectId?: string, limit = 20) => {
      const q = new URLSearchParams({ limit: String(limit) })
      if (projectId) q.set('project_id', projectId)
      return request<any>(`/vercel/deployments?${q}`)
    },
    deployment: (id: string) => request<any>(`/vercel/deployments/${id}`),
    logs: (id: string, limit = 200) => request<any>(`/vercel/deployments/${id}/logs?limit=${limit}`),
  },
  railway: {
    status: () => request<any>('/railway/status'),
    connect: (token: string) => request<any>('/railway/connect', { method: 'POST', body: JSON.stringify({ token }) }),
    disconnect: () => request<any>('/railway/disconnect', { method: 'DELETE' }),
    projects: () => request<any>('/railway/projects'),
    projectDeployments: (projectId: string) => request<any>(`/railway/projects/${projectId}/deployments`),
  },
deployWatch: {
    vercel: (projectId?: string) =>
      request<any>(`/deploy-watch/vercel${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''}`),
    railway: (projectId?: string) =>
      request<any>(`/deploy-watch/railway${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''}`),
  },
}
// ─── Image Studio (GPT image generation & editing) ─────────────────────────
export interface ImageStudioResult {
  ok: boolean
  image_base64?: string
  image_mime?: string
  text?: string
  error_code?: string
  detail?: string
  response_id?: string // OpenAI response id — chain follow-up edits onto it
}

/** Calls the Image Studio backend. Errors come back as { ok:false, detail }
 *  rather than thrown, except for network/auth failures thrown by request(). */
export function generateImage(body: {
  prompt: string
  model?: string // gpt-5.5 (default) | gpt-5.6-sol | gpt-5.6-terra | gpt-5.6-luna
  images?: { base64: string; mime: string }[] // up to 5 reference images
  image_base64?: string // legacy single-image compat
  image_mime?: string
  quality?: string // low | medium | high — omitted means auto
  size?: string // auto | 1024x1024 | 1536x1024 | 1024x1536
  output_format?: string // png | jpeg | webp
  previous_response_id?: string // multi-turn editing: edit the previous result
}): Promise<ImageStudioResult> {
  return request<ImageStudioResult>('/images/generate', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}
