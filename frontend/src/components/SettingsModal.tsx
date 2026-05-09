import React, { useState, useEffect } from 'react'
import { useAppStore } from '../stores/appStore'
import { api } from '../api/client'
import { toast } from '../lib/toast'
import { X, Eye, EyeOff, CheckCircle, Key, Brain, FolderOpen, Code, Cpu, Sliders, Users, Sun, Moon, Github, ExternalLink, AlertCircle } from 'lucide-react'
import { AdminUsersPanel } from './AdminUsersPanel'
import { useAuthStore } from '../stores/authStore'
import { useThemeStore } from '../stores/themeStore'

type Tab = 'api' | 'models' | 'workspace' | 'editor' | 'local' | 'users' | 'github'

const TABS: { id: Tab; icon: React.ReactNode; label: string }[] = [
  { id: 'api',       icon: <Key size={14} />,       label: 'API Keys' },
  { id: 'models',    icon: <Brain size={14} />,      label: 'Models' },
  { id: 'github',    icon: <Github size={14} />,     label: 'GitHub' },
  { id: 'workspace', icon: <FolderOpen size={14} />, label: 'Workspace' },
  { id: 'editor',    icon: <Code size={14} />,       label: 'Editor' },
  { id: 'local',     icon: <Cpu size={14} />,        label: 'Local AI' },
  { id: 'users',    icon: <Users size={14} />,     label: 'Users' },
]

export function SettingsModal() {
  const { settingsOpen, setSettingsOpen, settings, setSettings } = useAppStore()
  const [tab, setTab] = useState<Tab>('api')
  const [apiKey, setApiKey] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [verifying, setVerifying] = useState(false)
  const [keyStatus, setKeyStatus] = useState<'idle' | 'ok' | 'error'>('idle')
  const [keyMessage, setKeyMessage] = useState('')
  const [anthropicKey, setAnthropicKey] = useState('')
  const [showAnthropicKey, setShowAnthropicKey] = useState(false)
  const [anthropicVerifying, setAnthropicVerifying] = useState(false)
  const [anthropicStatus, setAnthropicStatus] = useState<'idle' | 'ok' | 'error'>('idle')
  const [anthropicMessage, setAnthropicMessage] = useState('')
  const [geminiKey, setGeminiKey] = useState('')
  const [showGeminiKey, setShowGeminiKey] = useState(false)
  const [geminiVerifying, setGeminiVerifying] = useState(false)
  const [geminiStatus, setGeminiStatus] = useState<'idle' | 'ok' | 'error'>('idle')
  const [geminiMessage, setGeminiMessage] = useState('')
  const [geminiConnected, setGeminiConnected] = useState(false)
  const [githubPat, setGithubPat] = useState('')
  const [showGithubPat, setShowGithubPat] = useState(false)
  const [githubConnecting, setGithubConnecting] = useState(false)
  const [githubStatus, setGithubStatus] = useState<any>(null)
  const [githubStatusMsg, setGithubStatusMsg] = useState('')
  const [models, setModels] = useState<any[]>([])
  const [form, setForm] = useState({
    architect_model: 'gpt-5',
    surgeon_model: 'gpt-4.1',
    confidence_threshold: 7,
    temperature_architect: 0.3,
    temperature_surgeon: 0.1,
    auto_backup: true,
    font_size: 14,
    workspace_path: '',
    ollama_enabled: false,
    ollama_base_url: 'http://localhost:11434',
    ollama_model: 'qwen2.5-coder:7b',
  })

  const { theme, setTheme } = useThemeStore()

  useEffect(() => {
    if (settings) {
      setForm({
        architect_model: settings.architect_model,
        surgeon_model: settings.surgeon_model,
        confidence_threshold: settings.confidence_threshold,
        temperature_architect: settings.temperature_architect,
        temperature_surgeon: settings.temperature_surgeon,
        auto_backup: settings.auto_backup,
        font_size: settings.font_size,
        workspace_path: settings.workspace_path,
        ollama_enabled: (settings as any).ollama_enabled || false,
        ollama_base_url: (settings as any).ollama_base_url || 'http://localhost:11434',
        ollama_model: (settings as any).ollama_model || 'qwen2.5-coder:7b',
      })
    }
    api.settings.getModels().then((d) => setModels(d.models || [])).catch(() => {})
    try { (api as any).github.status().then((s: any) => setGithubStatus(s)).catch(() => {}) } catch(_) {}
    try { api.settings.geminiStatus().then((s: any) => setGeminiConnected(s?.connected || false)).catch(() => {}) } catch(_) {}
  }, [settings, settingsOpen])

  const handleVerifyGeminiKey = async () => {
    if (!geminiKey.trim()) return
    setGeminiVerifying(true)
    setGeminiStatus('idle')
    setGeminiMessage('')
    try {
      const res: any = await api.settings.verifyGeminiKey(geminiKey.trim())
      setGeminiStatus('ok')
      setGeminiMessage(res.message || 'Gemini API key verified!')
      setGeminiConnected(true)
      setGeminiKey('')
    } catch (e: any) {
      setGeminiStatus('error')
      setGeminiMessage(e.message || 'Invalid Gemini API key')
    } finally {
      setGeminiVerifying(false)
    }
  }

  const handleConnectGithub = async () => {
    if (!githubPat.trim()) return
    setGithubConnecting(true)
    setGithubStatusMsg('')
    try {
      const res: any = await (api as any).github.connect(githubPat.trim())
      setGithubStatus({ connected: true, username: res.username, avatar_url: res.avatar_url })
      setGithubStatusMsg('Connected successfully!')
      setGithubPat('')
    } catch (e: any) {
      setGithubStatusMsg(e.message || 'Connection failed')
    } finally {
      setGithubConnecting(false)
    }
  }

  const handleDisconnectGithub = async () => {
    try {
      await (api as any).github.disconnect()
      setGithubStatus({ connected: false })
      setGithubStatusMsg('Disconnected')
    } catch (e: any) {
      setGithubStatusMsg(e.message || 'Failed to disconnect')
    }
  }

  if (!settingsOpen) return null

  const handleVerifyKey = async () => {
    if (!apiKey.trim()) return
    setVerifying(true); setKeyStatus('idle')
    try {
      await api.settings.verifyKey(apiKey.trim())
      setKeyStatus('ok'); setKeyMessage('API key verified and saved!')
      const updated = await api.settings.get()
      setSettings(updated)
      toast.success('API key saved', 'OpenAI key verified successfully')
    } catch (e: any) {
      setKeyStatus('error'); setKeyMessage(e.message)
      toast.error('Key verification failed', e.message)
    }
    setVerifying(false)
  }

  const handleVerifyAnthropicKey = async () => {
    if (!anthropicKey.trim()) return
    setAnthropicVerifying(true); setAnthropicStatus('idle')
    try {
      await api.settings.verifyAnthropicKey(anthropicKey.trim())
      setAnthropicStatus('ok'); setAnthropicMessage('Anthropic key verified and saved!')
      const updated = await api.settings.get()
      setSettings(updated)
      toast.success('Anthropic key saved', 'Claude models now available')
    } catch (e: any) {
      setAnthropicStatus('error'); setAnthropicMessage(e.message)
      toast.error('Anthropic key failed', e.message)
    }
    setAnthropicVerifying(false)
  }

  const handleSave = async () => {
    try {
      // If user typed an API key, verify+save it as part of the save flow
      if (apiKey.trim()) {
        setVerifying(true)
        try {
          await api.settings.verifyKey(apiKey.trim())
          setKeyStatus('ok')
          setKeyMessage('API key verified and saved!')
        } catch (e: any) {
          setVerifying(false)
          setKeyStatus('error')
          setKeyMessage(e.message)
          toast.error('API key invalid', e.message)
          return // Don't save other settings if key is bad
        }
        setVerifying(false)
      }
      // If user typed an Anthropic key, verify it too
      if (anthropicKey.trim()) {
        try {
          await api.settings.verifyAnthropicKey(anthropicKey.trim())
          setAnthropicStatus('ok')
        } catch (e: any) {
          setAnthropicStatus('error')
          setAnthropicMessage(e.message)
          toast.error('Anthropic key invalid', e.message)
          return
        }
      }
      await api.settings.update(form)
      const updated = await api.settings.get()
      setSettings(updated)
      setSettingsOpen(false)
      toast.success('Settings saved')
    } catch (e: any) {
      toast.error('Save failed', e.message)
    }
  }

  const upd = (k: string) => (v: any) => setForm((s) => ({ ...s, [k]: v }))

  return (
    <div
      className="fixed inset-0 bg-black/70 z-[100] flex items-center justify-center backdrop-blur-sm"
      onClick={() => setSettingsOpen(false)}
    >
      <div
        className="bg-surface border border-border rounded-2xl w-[580px] max-h-[85vh] flex flex-col shadow-modal animate-slide-up"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-border flex-shrink-0">
          <div>
            <div className="text-base font-bold text-ink">Settings</div>
            <div className="text-xs text-muted mt-0.5">All data stored locally — nothing leaves your machine</div>
          </div>
          <button onClick={() => setSettingsOpen(false)} className="btn-icon"><X size={18} /></button>
        </div>

        <div className="flex flex-1 min-h-0 overflow-hidden">
          {/* Left tabs */}
          <div className="w-36 flex-shrink-0 border-r border-border py-2">
            {TABS.map(({ id, icon, label }) => (
              <button
                key={id}
                onClick={() => setTab(id)}
                className={`w-full flex items-center gap-2.5 px-4 py-2.5 text-sm text-left transition-colors ${
                  tab === id
                    ? 'bg-overlay text-ink font-semibold border-r-2 border-accent'
                    : 'text-muted hover:text-ink hover:bg-overlay/50'
                }`}
              >
                <span className="flex-shrink-0">{icon}</span>
                {label}
              </button>
            ))}
          </div>

          {/* Tab content */}
          <div className="flex-1 overflow-y-auto p-6 min-w-0">
            {tab === 'api' && (
              <div className="space-y-4">
                <SectionHeader title="OpenAI API Key" subtitle="Required for all AI features" />
                <div>
                  <div className="flex gap-2">
                    <div className="relative flex-1">
                      <input
                        type={showKey ? 'text' : 'password'}
                        value={apiKey}
                        onChange={(e) => { setApiKey(e.target.value); setKeyStatus('idle') }}
                        onPaste={(e) => {
                          // Explicit paste handler — ensures pasted text always lands
                          e.preventDefault()
                          const pasted = e.clipboardData.getData('text').trim()
                          if (pasted) { setApiKey(pasted); setKeyStatus('idle') }
                        }}
                        placeholder={settings?.openai_api_key_set ? '••••••••••••••••••' : 'sk-proj-…'}
                        className={`input pr-10 ${keyStatus === 'ok' ? 'border-success focus:border-success' : keyStatus === 'error' ? 'border-danger' : ''}`}
                        onKeyDown={(e) => e.key === 'Enter' && handleVerifyKey()}
                        autoComplete="off"
                        spellCheck={false}
                      />
                      <button
                        onClick={() => setShowKey(!showKey)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-faint hover:text-muted"
                      >
                        {showKey ? <EyeOff size={14} /> : <Eye size={14} />}
                      </button>
                    </div>
                    <button
                      onClick={handleVerifyKey}
                      disabled={verifying || !apiKey.trim()}
                      className="btn-primary px-5 disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      {verifying ? '…' : 'Verify'}
                    </button>
                  </div>
                  {settings?.openai_api_key_set && keyStatus === 'idle' && (
                    <div className="flex items-center gap-1.5 mt-2 text-success text-xs">
                      <CheckCircle size={12} /> API key configured
                    </div>
                  )}
                  {keyStatus === 'ok' && <div className="text-success text-xs mt-2">{keyMessage}</div>}
                  {keyStatus === 'error' && <div className="text-danger text-xs mt-2">{keyMessage}</div>}
                  <div className="text-[11px] text-faint mt-2">
                    Get your key at{' '}
                    <a href="https://platform.openai.com/api-keys" target="_blank" rel="noopener" className="text-accent hover:underline">
                      platform.openai.com/api-keys
                    </a>
                  </div>
                </div>

                {/* ── Anthropic / Claude Key ── */}
                <div className="mt-6 pt-5 border-t border-border">
                  <SectionHeader title="Anthropic API Key (Claude)" subtitle="Enables Claude models as Architect — with visible thinking" />
                  <div className="mt-3">
                    <div className="flex gap-2">
                      <div className="relative flex-1">
                        <input
                          type={showAnthropicKey ? 'text' : 'password'}
                          value={anthropicKey}
                          onChange={(e) => { setAnthropicKey(e.target.value); setAnthropicStatus('idle') }}
                          onPaste={(e) => {
                            e.preventDefault()
                            const pasted = e.clipboardData.getData('text').trim()
                            if (pasted) { setAnthropicKey(pasted); setAnthropicStatus('idle') }
                          }}
                          placeholder={settings?.anthropic_api_key_set ? '••••••••••••••••••' : 'sk-ant-api03-…'}
                          className={`input pr-10 ${anthropicStatus === 'ok' ? 'border-success focus:border-success' : anthropicStatus === 'error' ? 'border-danger' : ''}`}
                          onKeyDown={(e) => e.key === 'Enter' && handleVerifyAnthropicKey()}
                          autoComplete="off"
                          spellCheck={false}
                        />
                        <button
                          onClick={() => setShowAnthropicKey(!showAnthropicKey)}
                          className="absolute right-3 top-1/2 -translate-y-1/2 text-faint hover:text-muted"
                        >
                          {showAnthropicKey ? <EyeOff size={14} /> : <Eye size={14} />}
                        </button>
                      </div>
                      <button
                        onClick={handleVerifyAnthropicKey}
                        disabled={anthropicVerifying || !anthropicKey.trim()}
                        className="btn-primary px-5 disabled:opacity-40 disabled:cursor-not-allowed"
                      >
                        {anthropicVerifying ? '…' : 'Verify'}
                      </button>
                    </div>
                    {settings?.anthropic_api_key_set && anthropicStatus === 'idle' && (
                      <div className="flex items-center gap-1.5 mt-2 text-success text-xs">
                        <CheckCircle size={12} /> Claude API key configured
                      </div>
                    )}
                    {anthropicStatus === 'ok' && <div className="text-success text-xs mt-2">{anthropicMessage}</div>}
                    {anthropicStatus === 'error' && <div className="text-danger text-xs mt-2">{anthropicMessage}</div>}
                    <div className="text-[11px] text-faint mt-2">
                      Get your key at{' '}
                      <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noopener" className="text-accent hover:underline">
                        console.anthropic.com
                      </a>
                      {' '}— enables Claude Sonnet 4 & Opus 4 with visible thinking
                    </div>
                  </div>
                </div>

                {/* ── Google Gemini Key ── */}
                <div className="mt-6 pt-5 border-t border-border">
                  <SectionHeader title="Google Gemini API Key" subtitle="Enables Gemini 2.5 Pro/Flash — 1M context window with visible thinking" />
                  <div className="mt-3">
                    <div className="flex gap-2">
                      <div className="relative flex-1">
                        <input
                          type={showGeminiKey ? 'text' : 'password'}
                          value={geminiKey}
                          onChange={(e) => { setGeminiKey(e.target.value); setGeminiStatus('idle') }}
                          onPaste={(e) => {
                            e.preventDefault()
                            const pasted = e.clipboardData.getData('text').trim()
                            if (pasted) { setGeminiKey(pasted); setGeminiStatus('idle') }
                          }}
                          placeholder={geminiConnected ? '••••••••••••••••••' : 'AIza…'}
                          className={`input pr-10 ${geminiStatus === 'ok' ? 'border-success focus:border-success' : geminiStatus === 'error' ? 'border-danger' : ''}`}
                          onKeyDown={(e) => e.key === 'Enter' && handleVerifyGeminiKey()}
                          autoComplete="off"
                          spellCheck={false}
                        />
                        <button
                          onClick={() => setShowGeminiKey(!showGeminiKey)}
                          className="absolute right-3 top-1/2 -translate-y-1/2 text-faint hover:text-muted"
                        >
                          {showGeminiKey ? <EyeOff size={14} /> : <Eye size={14} />}
                        </button>
                      </div>
                      <button
                        onClick={handleVerifyGeminiKey}
                        disabled={geminiVerifying || !geminiKey.trim()}
                        className="btn-primary px-5 disabled:opacity-40 disabled:cursor-not-allowed"
                      >
                        {geminiVerifying ? '…' : 'Verify'}
                      </button>
                    </div>
                    {geminiConnected && geminiStatus === 'idle' && (
                      <div className="flex items-center gap-1.5 mt-2 text-success text-xs">
                        <CheckCircle size={12} /> Gemini API key configured
                      </div>
                    )}
                    {geminiStatus === 'ok' && <div className="text-success text-xs mt-2">{geminiMessage}</div>}
                    {geminiStatus === 'error' && <div className="text-danger text-xs mt-2">{geminiMessage}</div>}
                    <div className="text-[11px] text-faint mt-2">
                      Get your key at{' '}
                      <a href="https://aistudio.google.com/app/apikey" target="_blank" rel="noopener" className="text-accent hover:underline">
                        aistudio.google.com
                      </a>
                      {' '}— enables Gemini 2.5 Pro (1M context) and Gemini 2.5 Flash
                    </div>
                  </div>
                </div>
              </div>
            )}

            {tab === 'models' && (
              <div className="space-y-5">
                <SectionHeader title="Model Configuration" subtitle="Architect plans the change · Surgeon writes the code" />

                <Field label="Architect Model (planning & analysis)">
                  <Select value={form.architect_model} onChange={upd('architect_model')} options={models.map((m) => ({ value: m.id, label: `${m.name} — ${m.description}` }))} />
                </Field>
                <Field label="Surgeon Model (code writing)">
                  <Select value={form.surgeon_model} onChange={upd('surgeon_model')} options={models.map((m) => ({ value: m.id, label: `${m.name} — ${m.description}` }))} />
                </Field>

                <div className="grid grid-cols-2 gap-4">
                  <Field label={`Architect Temp: ${form.temperature_architect}`}>
                    <input type="range" min="0" max="1" step="0.1" value={form.temperature_architect}
                      onChange={(e) => upd('temperature_architect')(parseFloat(e.target.value))} className="w-full accent-accent" />
                  </Field>
                  <Field label={`Surgeon Temp: ${form.temperature_surgeon}`}>
                    <input type="range" min="0" max="0.5" step="0.05" value={form.temperature_surgeon}
                      onChange={(e) => upd('temperature_surgeon')(parseFloat(e.target.value))} className="w-full accent-success" />
                  </Field>
                </div>

                <Field label={`Confidence Threshold: ${form.confidence_threshold}/10 (lower = more auto-applies)`}>
                  <input type="range" min="1" max="10" step="1" value={form.confidence_threshold}
                    onChange={(e) => upd('confidence_threshold')(parseInt(e.target.value))} className="w-full accent-warning" />
                  <div className="flex justify-between text-[10px] text-faint mt-0.5">
                    <span>1 — auto-apply all</span><span>10 — always manual review</span>
                  </div>
                </Field>
              </div>
            )}

            {tab === 'workspace' && (
              <div className="space-y-4">
                <SectionHeader title="Workspace" subtitle="Default project folder for the file browser" />
                <Field label="Default workspace path">
                  <input
                    value={form.workspace_path}
                    onChange={(e) => upd('workspace_path')(e.target.value)}
                    placeholder="/home/user/projects/my-app"
                    className="input"
                  />
                  <div className="text-[11px] text-faint mt-1">Opens automatically when the app starts</div>
                </Field>
              </div>
            )}

            {tab === 'editor' && (
              <div className="space-y-5">
                {/* Theme */}
                <div className="space-y-2">
                  <label className="text-xs font-semibold text-ink">Theme</label>
                  <div className="flex gap-2">
                    <button
                      onClick={() => setTheme('dark')}
                      className={`flex items-center gap-2 px-3 py-2 rounded-lg border text-xs font-medium transition ${
                        theme === 'dark'
                          ? 'border-accent bg-accent/10 text-accent'
                          : 'border-border bg-surface text-muted hover:text-ink'
                      }`}
                    >
                      <Moon size={14} /> Dark
                    </button>
                    <button
                      onClick={() => setTheme('light')}
                      className={`flex items-center gap-2 px-3 py-2 rounded-lg border text-xs font-medium transition ${
                        theme === 'light'
                          ? 'border-accent bg-accent/10 text-accent'
                          : 'border-border bg-surface text-muted hover:text-ink'
                      }`}
                    >
                      <Sun size={14} /> Light
                    </button>
                  </div>
                </div>

                <SectionHeader title="Editor" subtitle="Monaco editor preferences" />
                <Field label={`Font size: ${form.font_size}px`}>
                  <input type="range" min="11" max="20" step="1" value={form.font_size}
                    onChange={(e) => upd('font_size')(parseInt(e.target.value))} className="w-full accent-accent" />
                  <div className="flex justify-between text-[10px] text-faint mt-0.5"><span>11px</span><span>20px</span></div>
                </Field>
                <label className="flex items-start gap-3 cursor-pointer group">
                  <input type="checkbox" checked={form.auto_backup} onChange={(e) => upd('auto_backup')(e.target.checked)}
                    className="mt-0.5 accent-accent" />
                  <div>
                    <div className="text-sm font-medium text-ink">Auto-backup before every surgical change</div>
                    <div className="text-xs text-muted mt-0.5">Saves to <code className="font-mono text-[10px] bg-overlay px-1 rounded">.surgicalai_backups/</code> — always reversible</div>
                  </div>
                </label>
              </div>
            )}

            {tab === 'local' && (
              <div className="space-y-5">
                <SectionHeader title="Local AI via Ollama" subtitle="Run models locally — no OpenAI needed, full privacy" />
                <label className="flex items-start gap-3 cursor-pointer">
                  <input type="checkbox" checked={form.ollama_enabled || false} onChange={(e) => upd('ollama_enabled')(e.target.checked)} className="mt-0.5 accent-accent" />
                  <div>
                    <div className="text-sm font-medium text-ink">Enable Ollama (local models)</div>
                    <div className="text-xs text-muted mt-0.5">Falls back to Ollama when OpenAI key is not set</div>
                  </div>
                </label>

                {form.ollama_enabled && (
                  <div className="space-y-4">
                    <Field label="Ollama base URL">
                      <input value={form.ollama_base_url || 'http://localhost:11434'} onChange={(e) => upd('ollama_base_url')(e.target.value)} className="input" />
                    </Field>
                    <Field label="Default model">
                      <input value={form.ollama_model || 'qwen2.5-coder:7b'} onChange={(e) => upd('ollama_model')(e.target.value)} placeholder="qwen2.5-coder:7b" className="input" />
                    </Field>
                    <div className="p-3 bg-base rounded-lg border border-border text-xs text-muted leading-relaxed">
                      Install Ollama at <a href="https://ollama.ai" target="_blank" rel="noopener" className="text-accent">ollama.ai</a>. 
                      Recommended models: <code className="text-accent">qwen2.5-coder:7b</code>, <code className="text-accent">codellama:13b</code>, <code className="text-accent">deepseek-coder-v2</code>
                    </div>
                  </div>
                )}
              </div>
            )}
            {tab === 'users' && (
              <div className="space-y-5">
                <AdminUsersPanel />
              </div>
            )}

            {tab === 'github' && (
              <div className="space-y-5">
                <SectionHeader
                  title="GitHub Integration"
                  subtitle="Connect your GitHub account to browse repos, load files, and push commits directly from SurgicalAI"
                />

                {/* Connected state */}
                {githubStatus?.connected ? (
                  <div className="space-y-4">
                    <div className="flex items-center gap-3 p-3 rounded-lg border border-green-500/30 bg-green-500/10">
                      {githubStatus.avatar_url && (
                        <img src={githubStatus.avatar_url} alt="avatar" className="w-9 h-9 rounded-full border border-border" />
                      )}
                      <div className="flex-1 min-w-0">
                        <div className="text-sm font-semibold text-green-400">Connected as @{githubStatus.username}</div>
                        <div className="text-xs text-muted">Your repositories are available in the GitHub sidebar tab</div>
                      </div>
                      <button
                        onClick={handleDisconnectGithub}
                        className="text-xs text-red-400 hover:text-red-300 border border-red-500/30 rounded px-3 py-1.5 hover:bg-red-500/10 transition-colors"
                      >
                        Disconnect
                      </button>
                    </div>
                    {githubStatusMsg && (
                      <div className="text-xs text-muted">{githubStatusMsg}</div>
                    )}
                  </div>
                ) : (
                  /* Not connected state */
                  <div className="space-y-4">
                    <Field label="Personal Access Token (Classic)">
                      <div className="flex gap-2">
                        <input
                          type="password"
                          className="input flex-1"
                          placeholder="ghp_xxxxxxxxxxxxxxxxxxxx"
                          value={githubPat}
                          onChange={(e) => setGithubPat(e.target.value)}
                          onKeyDown={(e) => e.key === 'Enter' && handleConnectGithub()}
                        />
                        <button
                          onClick={handleConnectGithub}
                          disabled={githubConnecting || !githubPat.trim()}
                          className="btn-primary px-4 text-sm disabled:opacity-50"
                        >
                          {githubConnecting ? 'Connecting…' : 'Connect'}
                        </button>
                      </div>
                    </Field>

                    {githubStatusMsg && (
                      <div className={`text-xs px-3 py-2 rounded ${githubStatusMsg.includes('success') ? 'text-green-400 bg-green-500/10' : 'text-red-400 bg-red-500/10'}`}>
                        {githubStatusMsg}
                      </div>
                    )}

                    <div className="text-xs text-muted space-y-1 p-3 rounded-lg bg-surface border border-border">
                      <div className="font-medium text-ink mb-2">How to get a token:</div>
                      <div>1. Go to <a href="https://github.com/settings/tokens" target="_blank" rel="noreferrer" className="text-blue-400 hover:underline">github.com/settings/tokens</a></div>
                      <div>2. Click <strong className="text-ink">"Generate new token (classic)"</strong></div>
                      <div>3. Select the <strong className="text-ink">repo</strong> and <strong className="text-ink">read:user</strong> scopes</div>
                      <div>4. Copy the token and paste it above</div>
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-border flex-shrink-0">
          <button onClick={() => setSettingsOpen(false)} className="btn-ghost border border-border px-5 py-2 text-sm">Cancel</button>
          {tab !== 'users' && <button onClick={handleSave} className="btn-primary px-6">Save Settings</button>}
        </div>
      </div>
    </div>
  )
}

function SectionHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="mb-1">
      <div className="text-sm font-bold text-ink">{title}</div>
      {subtitle && <div className="text-xs text-muted mt-0.5">{subtitle}</div>}
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-xs text-muted mb-1.5 font-medium">{label}</div>
      {children}
    </div>
  )
}

function Select({ value, onChange, options }: { value: string; onChange: (v: string) => void; options: { value: string; label: string }[] }) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="input"
    >
      {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  )
}
