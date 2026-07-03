import type { StreamChunk, ProjectMemory, PromptTemplate, ImpactAnalysis, MultiFileAnalysis, MemoryPreset } from '../types'

const BASE = (import.meta.env.VITE_API_URL ?? '') + '/api'

/** Read JWT from persisted auth store without importing zustand (avoids circular deps).
 *  Auth is stored under `surgicalai-auth-{username}` (namespaced) or legacy `surgicalai-auth`. */
function getAuthToken(): string | null {
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
    throw new Error(err.detail || `HTTP ${res.status}`)
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
      data: { session_id: string; message: string; file_ids?: string[] },
      onProgress: (msg: string) => void,
      onToken: (token: string) => void,
      onResult: (result: any) => void,
      onDone: (fullText: string) => void,
      onError: (err: string) => void,
      onThinking?: (text: string, phase: 'start' | 'delta' | 'end') => void,
      onCompacting?: (phase: 'start' | 'done') => void,
      onEditStart?: () => void,
      onEditEnd?: () => void,
      onTask?: (event: any) => void
    ): AbortController => {
      const controller = new AbortController()
      const tokens: string[] = []
      let doneCalled = false
      const fireDone = () => { if (!doneCalled) { doneCalled = true; onDone(tokens.join('')) } }

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

        const processLine = (line: string) => {
          if (!line.startsWith('data: ')) return
          try {
            const chunk = JSON.parse(line.slice(6))
            if (chunk.type === 'progress') onProgress(chunk.content)
            else if (chunk.type === 'token') { tokens.push(chunk.content); onToken(chunk.content) }
            else if (chunk.type === 'smart_result') {
              // Natural pipeline: result may include natural_text already streamed as tokens
              const result = JSON.parse(chunk.content)
              onResult(result)
            }
            else if (chunk.type === 'chat') { tokens.push(chunk.content); onToken(chunk.content) }
            else if (chunk.type === 'done') fireDone()
            else if (chunk.type === 'error') onError(chunk.content)
            else if (chunk.type === 'thinking_start') onThinking?.('', 'start')
            else if (chunk.type === 'thinking') onThinking?.(chunk.content, 'delta')
            else if (chunk.type === 'thinking_end') onThinking?.('', 'end')
            else if (chunk.type === 'compacting') onCompacting?.('start')
            else if (chunk.type === 'compacting_done') onCompacting?.('done')
            else if (chunk.type === 'edit_start') onEditStart?.()
            else if (chunk.type === 'edit_end') onEditEnd?.()
            else if (
              chunk.type === 'planning_started' ||
              chunk.type === 'task_plan' || chunk.type === 'task_start' ||
              chunk.type === 'task_progress' || chunk.type === 'task_done' ||
              chunk.type === 'task_blocked' || chunk.type === 'task_cancelled' ||
              chunk.type === 'tasks_complete'
            ) onTask?.(chunk)
          } catch {}
        }

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

        const processLine = (line: string) => {
          if (!line.startsWith('data: ')) return
          try {
            const chunk = JSON.parse(line.slice(6))
            if (chunk.type === 'smart_result') onResult(JSON.parse(chunk.content))
            else if (chunk.type === 'done') fireDone()
            else if (chunk.type === 'error') onError(chunk.content)
            else if (
              chunk.type === 'task_start' || chunk.type === 'task_progress' ||
              chunk.type === 'task_done' || chunk.type === 'task_blocked' ||
              chunk.type === 'task_cancelled' || chunk.type === 'tasks_complete'
            ) {
              if (chunk.type === 'task_progress') onProgress(chunk.content)
              onTask(chunk)
            }
          } catch {}
        }

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
          throw new Error(err.detail || `HTTP ${res.status}`)
        }
        return res.json()
      })
    },
    list: (sessionId: string) =>
      request<any[]>(`/chat/${sessionId}/files`),
    get: (sessionId: string, fileId: string) =>
      request<any>(`/chat/${sessionId}/files/${fileId}`),
    /** Resolve the full import graph (components + CSS + npm deps) for a live
     *  preview. Pass `content` to preview an unsaved/modified version. */
    previewBundle: (sessionId: string, fileId: string, content?: string) =>
      request<any>(`/chat/${sessionId}/files/${fileId}/preview-bundle`, {
        method: 'POST',
        body: JSON.stringify(content != null ? { content } : {}),
      }),
    update: (sessionId: string, fileId: string, content: string) =>
      request<any>(`/chat/${sessionId}/files/${fileId}`, { method: 'PUT', body: JSON.stringify({ content }) }),
    undo: (sessionId: string, fileId: string) =>
      request<any>(`/chat/${sessionId}/files/${fileId}/undo`, { method: 'POST' }),
    delete: (sessionId: string, fileId: string) =>
      request(`/chat/${sessionId}/files/${fileId}`, { method: 'DELETE' }),
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
}

/** Calls the Image Studio backend. Errors come back as { ok:false, detail }
 *  rather than thrown, except for network/auth failures thrown by request(). */
export function generateImage(body: {
  prompt: string
  image_base64?: string
  image_mime?: string
  quality?: string // low | medium | high — omitted means auto
}): Promise<ImageStudioResult> {
  return request<ImageStudioResult>('/images/generate', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}
