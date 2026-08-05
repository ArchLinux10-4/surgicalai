import React, { useState, useEffect } from 'react'
import { useAppStore } from '../stores/appStore'
import { api } from '../api/client'
import { toast } from '../lib/toast'
import { AdminUsersPanel } from './AdminUsersPanel'
import { GitHubAppPanel } from './GitHubAppPanel'
import { useAuthStore } from '../stores/authStore'
import { useThemeStore } from '../stores/themeStore'
import { clientLog } from '../lib/clientLog'
import { AttachMoney, BugReport, CheckCircle, Close, Code, DarkMode, ErrorOutline, FolderOpen, GitHub, Group, LightMode, Lock, Memory, OpenInNew, Psychology, Tune, Visibility, VisibilityOff, VpnKey } from '@mui/icons-material';

type Tab = 'api' | 'models' | 'workspace' | 'editor' | 'users' | 'github' | 'vercel' | 'railway' | 'security' | 'debug'

const TABS: { id: Tab; icon: React.ReactNode; label: string }[] = [
  { id: 'api',       icon: <VpnKey sx={{ fontSize: 14 }} />,       label: 'API Keys' },
  { id: 'models',    icon: <Psychology sx={{ fontSize: 14 }} />,      label: 'Models' },
  { id: 'github',    icon: <GitHub sx={{ fontSize: 14 }} />,     label: 'GitHub' },
  { id: 'vercel',    icon: <span style={{ fontSize: 12, fontWeight: 'bold' }}>▲</span>,     label: 'Vercel' },
  { id: 'railway',   icon: <span style={{ fontSize: 13, fontWeight: 'bold', color: '#dc2626' }}>⬡</span>,  label: 'Railway' },
  { id: 'workspace', icon: <FolderOpen sx={{ fontSize: 14 }} />, label: 'Workspace' },
  { id: 'editor',    icon: <Code sx={{ fontSize: 14 }} />,       label: 'Editor' },
  { id: 'users',     icon: <Group sx={{ fontSize: 14 }} />,      label: 'Users' },
  { id: 'security',  icon: <Lock sx={{ fontSize: 14 }} />,       label: 'Security' },
  { id: 'debug',     icon: <BugReport sx={{ fontSize: 14 }} />,   label: 'Debug Logs' },
]

export function SettingsModal() {
  const { settingsOpen, setSettingsOpen, settings, setSettings } = useAppStore()
  const { user } = useAuthStore()
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
  // xAI Grok key state — same per-provider hook set the OpenAI/Anthropic
  // sections use (there is no shared key-input abstraction in this file).
  const [grokKey, setGrokKey] = useState('')
  const [showGrokKey, setShowGrokKey] = useState(false)
  const [grokVerifying, setGrokVerifying] = useState(false)
  const [grokStatus, setGrokStatus] = useState<'idle' | 'ok' | 'error'>('idle')
  const [grokMessage, setGrokMessage] = useState('')
  const [grokConnected, setGrokConnected] = useState(false)
  const [githubPat, setGithubPat] = useState('')
  const [vercelToken, setVercelToken] = useState('')
  const [vercelConnecting, setVercelConnecting] = useState(false)
  const [vercelStatus, setVercelStatus] = useState<any>(null)
  const [vercelStatusMsg, setVercelStatusMsg] = useState('')
  const [railwayToken, setRailwayToken] = useState('')
  const [railwayConnecting, setRailwayConnecting] = useState(false)
  const [railwayStatus, setRailwayStatus] = useState<any>(null)
  const [railwayStatusMsg, setRailwayStatusMsg] = useState('')
  const [showGithubPat, setShowGithubPat] = useState(false)
  const [githubConnecting, setGithubConnecting] = useState(false)
  const [githubStatus, setGithubStatus] = useState<any>(null)
  const [githubStatusMsg, setGithubStatusMsg] = useState('')
  // models now comes from the shared appStore (see stores/appStore.ts) so
  // that verifying a key here also updates ChatPanel's inline model picker
  // immediately, without needing a page reload.
  const { availableModels: models, refreshModels } = useAppStore()
  const [browsingDir, setBrowsingDir] = useState(false)

  // Security tab — change password
  const [pwCurrent, setPwCurrent]         = useState('')
  const [pwNew, setPwNew]                 = useState('')
  const [pwConfirm, setPwConfirm]         = useState('')
  const [showPwCurrent, setShowPwCurrent] = useState(false)
  const [showPwNew, setShowPwNew]         = useState(false)
  const [showPwConfirm, setShowPwConfirm] = useState(false)
  const [pwSaving, setPwSaving]           = useState(false)
  const [pwResult, setPwResult]           = useState<{ ok: boolean; msg: string } | null>(null)
  const [form, setForm] = useState({
    architect_model: 'claude-sonnet-4-6',
    surgeon_model: 'claude-sonnet-4-6',
    confidence_threshold: 7,
    temperature_architect: 0.3,
    temperature_surgeon: 0.1,
    auto_backup: true,
    font_size: 14,
    workspace_path: '',
    ollama_enabled: false,
    ollama_base_url: 'http://localhost:11434',
    ollama_model: 'qwen2.5-coder:7b',
    // Claude native web-search — global default for Ask/Plan (see
    // services/claude_web_search.py). Edit/Agent mode has its own separate,
    // per-message "Research" checkbox in the chat composer, so this toggle
    // is scoped to "on by default for Ask/Plan" only.
    web_search_enabled: false,
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
        web_search_enabled: (settings as any).web_search_enabled || false,
      })
    }
    refreshModels()
    try { (api as any).github.status().then((s: any) => setGithubStatus(s)).catch(() => {}) } catch(_) {}
    try { (api as any).vercel.status().then((s: any) => setVercelStatus(s)).catch(() => {}) } catch(_) {}
    try { (api as any).railway.status().then((s: any) => setRailwayStatus(s)).catch(() => {}) } catch(_) {}
    try { api.settings.geminiStatus().then((s: any) => setGeminiConnected(s?.connected || false)).catch(() => {}) } catch(_) {}
    // /settings/grok-status returns both `configured` and `connected` (the
    // Gemini endpoint only returns `configured`, which is why the line above
    // can never light up) — read either so the indicator actually works.
    try {
      api.settings.grokStatus()
        .then((s: any) => {
          const on = !!(s?.configured || s?.connected)
          setGrokConnected(on)
          clientLog('grok_status_loaded', { configured: on })
        })
        .catch((e: any) => { clientLog('grok_status_failed', { error: String(e?.message || e).slice(0, 200) }) })
    } catch (_) {
      clientLog('grok_status_threw', {})
    }
  }, [settings, settingsOpen])

  const handleVerifyGrokKey = async () => {
    if (!grokKey.trim()) {
      clientLog('grok_key_verify_skipped_empty', {})
      return
    }
    setGrokVerifying(true)
    setGrokStatus('idle')
    setGrokMessage('')
    clientLog('grok_key_verify_started', { keyLength: grokKey.trim().length })
    try {
      const res: any = await api.settings.verifyGrokKey(grokKey.trim())
      setGrokStatus('ok')
      setGrokMessage(res?.message || 'xAI Grok API key verified!')
      setGrokConnected(true)
      setGrokKey('')
      clientLog('grok_key_verify_ok', {})
      refreshModels()
      toast.success('Grok key saved', 'Grok 4.5 is now available in the model picker')
    } catch (e: any) {
      setGrokStatus('error')
      setGrokMessage(e?.message || 'Invalid xAI Grok API key')
      clientLog('grok_key_verify_failed', { error: String(e?.message || e).slice(0, 200) })
      toast.error('Grok key failed', e?.message || 'Invalid xAI Grok API key')
    } finally {
      setGrokVerifying(false)
      clientLog('grok_key_verify_finished', {})
    }
  }

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
      clientLog('gemini_key_verify_ok', {})
      refreshModels()
    } catch (e: any) {
      setGeminiStatus('error')
      setGeminiMessage(e.message || 'Invalid Gemini API key')
    } finally {
      setGeminiVerifying(false)
    }
  }

  const handleConnectVercel = async () => {
    if (!vercelToken.trim()) return
    setVercelConnecting(true)
    try {
      const res: any = await (api as any).vercel.connect(vercelToken.trim())
      setVercelStatus({ connected: true, username: res.username, email: res.email, avatar_url: res.avatar_url })
      setVercelStatusMsg('Connected successfully!')
    } catch (e: any) {
      setVercelStatusMsg(e.message || 'Connection failed')
    } finally {
      setVercelConnecting(false)
    }
  }

  const handleDisconnectVercel = async () => {
    try {
      await (api as any).vercel.disconnect()
      setVercelStatus({ connected: false })
      setVercelStatusMsg('Disconnected')
    } catch (e: any) {
      setVercelStatusMsg(e.message || 'Failed to disconnect')
    }
  }

  const handleConnectRailway = async () => {
    if (!railwayToken.trim()) return
    setRailwayConnecting(true)
    try {
      const res: any = await (api as any).railway.connect(railwayToken.trim())
      setRailwayStatus({ connected: true, name: res.name, email: res.email })
      setRailwayStatusMsg('Connected successfully!')
    } catch (e: any) {
      setRailwayStatusMsg(e.message || 'Connection failed')
    } finally {
      setRailwayConnecting(false)
    }
  }

  const handleDisconnectRailway = async () => {
    try {
      await (api as any).railway.disconnect()
      setRailwayStatus({ connected: false })
      setRailwayStatusMsg('Disconnected')
    } catch (e: any) {
      setRailwayStatusMsg(e.message || 'Failed to disconnect')
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

  const handleBrowseDirectory = async () => {
    setBrowsingDir(true)
    try {
      const res: any = await api.settings.browseDirectory()
      if (res?.path) {
        setForm((s) => ({ ...s, workspace_path: res.path }))
        toast.success('Folder selected', res.path)
      }
    } catch (e: any) {
      toast.error('Could not open folder picker', e.message || 'Please type the path manually')
    } finally {
      setBrowsingDir(false)
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
      clientLog('openai_key_verify_ok', {})
      refreshModels()
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
      clientLog('anthropic_key_verify_ok', {})
      refreshModels()
      toast.success('Anthropic key saved', 'Claude models now available')
    } catch (e: any) {
      setAnthropicStatus('error'); setAnthropicMessage(e.message)
      toast.error('Anthropic key failed', e.message)
    }
    setAnthropicVerifying(false)
  }

  const handleSave = async () => {
    try {
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
          return
        }
        setVerifying(false)
      }
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
      if (apiKey.trim() || anthropicKey.trim()) {
        clientLog('settings_save_key_verified_refreshing_models', {})
        refreshModels()
      }
      setSettingsOpen(false)
      toast.success('Settings saved')
    } catch (e: any) {
      toast.error('Save failed', e.message)
    }
  }

  const upd = (k: string) => (v: any) => setForm((s) => ({ ...s, [k]: v }))

  const pwStrength = (pw: string): number => {
    if (!pw) return 0
    let score = 0
    if (pw.length >= 8)                    score++
    if (/[A-Z]/.test(pw))                  score++
    if (/[a-z]/.test(pw) && /[0-9]/.test(pw)) score++
    if (/[^A-Za-z0-9]/.test(pw))           score++
    return score
  }
  const pwStrengthLabel = ['', 'Weak', 'Fair', 'Good', 'Strong']
  const pwStrengthColor = ['', 'bg-red-500', 'bg-yellow-400', 'bg-green-400', 'bg-emerald-500']
  const pwStrengthText  = ['', 'text-red-400', 'text-yellow-400', 'text-green-400', 'text-emerald-400']

  const handleChangePassword = async () => {
    setPwResult(null)
    if (!pwCurrent || !pwNew || !pwConfirm) {
      setPwResult({ ok: false, msg: 'All three fields are required' })
      return
    }
    setPwSaving(true)
    try {
      await (api as any).auth.changePassword(pwCurrent, pwNew, pwConfirm)
      setPwResult({ ok: true, msg: 'Password updated successfully' })
      setPwCurrent(''); setPwNew(''); setPwConfirm('')
    } catch (err: any) {
      const detail = err?.detail || err?.message || 'Something went wrong — please try again'
      setPwResult({ ok: false, msg: detail })
    } finally {
      setPwSaving(false)
    }
  }

  return (
    <div
      className="fixed inset-0 bg-black/70 z-[100] flex items-center justify-center backdrop-blur-sm"
      onClick={() => setSettingsOpen(false)}
    >
      <div
        className="bg-surface border border-border rounded-2xl flex flex-col shadow-modal animate-slide-up
          w-[580px] max-h-[85vh]
          max-sm:w-full max-sm:h-full max-sm:max-h-full max-sm:rounded-none max-sm:border-0"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-border flex-shrink-0">
          <div>
            <div className="text-base font-bold text-ink">Settings</div>
            <div className="text-xs text-muted mt-0.5">All data stored locally — nothing leaves your machine</div>
          </div>
          <button onClick={() => setSettingsOpen(false)} className="btn-icon"><Close sx={{ fontSize: 18 }} /></button>
        </div>

        <div className="flex flex-1 min-h-0 overflow-hidden max-sm:flex-col">
          {/* Left tabs */}
          <div className="w-36 flex-shrink-0 border-r border-border py-2
            max-sm:w-full max-sm:border-r-0 max-sm:border-b max-sm:border-border max-sm:py-0
            max-sm:flex max-sm:flex-row max-sm:overflow-x-auto max-sm:flex-shrink-0">
            {TABS.filter(t => t.id !== 'debug' || user?.is_admin).map(({ id, icon, label }) => (
              <button
                key={id}
                onClick={() => setTab(id)}
                className={`w-full flex items-center gap-2.5 px-4 py-2.5 text-sm text-left transition-colors
                  max-sm:w-auto max-sm:flex-shrink-0 max-sm:flex-col max-sm:gap-1 max-sm:px-3 max-sm:py-2.5 max-sm:text-[10px] max-sm:items-center
                  ${tab === id
                    ? 'bg-overlay text-ink font-semibold border-r-2 border-accent max-sm:border-r-0 max-sm:border-b-2'
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
                {/* Anthropic / Claude Key */}
                <div>
                  <SectionHeader title="Anthropic API Key" subtitle="Required — SurgicalAI runs on Claude" />
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
                          placeholder={settings?.anthropic_api_key_set ? '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022' : 'sk-ant-api03-\u2026'}
                          className={`input pr-10 ${anthropicStatus === 'ok' ? 'border-success focus:border-success' : anthropicStatus === 'error' ? 'border-danger' : ''}`}
                          onKeyDown={(e) => e.key === 'Enter' && handleVerifyAnthropicKey()}
                          autoComplete="off"
                          spellCheck={false}
                        />
                        <button
                          onClick={() => setShowAnthropicKey(!showAnthropicKey)}
                          className="absolute right-3 top-1/2 -translate-y-1/2 text-faint hover:text-muted"
                        >
                          {showAnthropicKey ? <VisibilityOff sx={{ fontSize: 14 }} /> : <Visibility sx={{ fontSize: 14 }} />}
                        </button>
                      </div>
                      <button
                        onClick={handleVerifyAnthropicKey}
                        disabled={anthropicVerifying || !anthropicKey.trim()}
                        className="btn-primary px-5 disabled:opacity-40 disabled:cursor-not-allowed"
                      >
                        {anthropicVerifying ? '\u2026' : 'Verify'}
                      </button>
                    </div>
                    {settings?.anthropic_api_key_set && anthropicStatus === 'idle' && (
                      <div className="flex items-center gap-1.5 mt-2 text-success text-xs">
                        <CheckCircle sx={{ fontSize: 12 }} /> Claude API key configured
                      </div>
                    )}
                    {anthropicStatus === 'ok' && <div className="text-success text-xs mt-2">{anthropicMessage}</div>}
                    {anthropicStatus === 'error' && <div className="text-danger text-xs mt-2">{anthropicMessage}</div>}
                    <div className="text-[11px] text-faint mt-2">
                      Get your key at{' '}
                      <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noopener" className="text-accent hover:underline">
                        console.anthropic.com
                      </a>
                      {' '}\u2014 enables Claude Sonnet 4 & Opus 4 with visible thinking
                    </div>
                  </div>
                </div>

                {/* OpenAI Key */}
                <div className="mt-6 pt-5 border-t border-border">
                  <SectionHeader title="OpenAI API Key" subtitle="Optional — enables GPT models" />
                  <div className="mt-3">
                    <div className="flex gap-2">
                      <div className="relative flex-1">
                        <input
                          type={showKey ? 'text' : 'password'}
                          value={apiKey}
                          onChange={(e) => { setApiKey(e.target.value); setKeyStatus('idle') }}
                          onPaste={(e) => {
                            e.preventDefault()
                            const pasted = e.clipboardData.getData('text').trim()
                            if (pasted) { setApiKey(pasted); setKeyStatus('idle') }
                          }}
                          placeholder={settings?.openai_api_key_set ? '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022' : 'sk-proj-\u2026'}
                          className={`input pr-10 ${keyStatus === 'ok' ? 'border-success focus:border-success' : keyStatus === 'error' ? 'border-danger' : ''}`}
                          onKeyDown={(e) => e.key === 'Enter' && handleVerifyKey()}
                          autoComplete="off"
                          spellCheck={false}
                        />
                        <button
                          onClick={() => setShowKey(!showKey)}
                          className="absolute right-3 top-1/2 -translate-y-1/2 text-faint hover:text-muted"
                        >
                          {showKey ? <VisibilityOff sx={{ fontSize: 14 }} /> : <Visibility sx={{ fontSize: 14 }} />}
                        </button>
                      </div>
                      <button
                        onClick={handleVerifyKey}
                        disabled={verifying || !apiKey.trim()}
                        className="btn-primary px-5 disabled:opacity-40 disabled:cursor-not-allowed"
                      >
                        {verifying ? '\u2026' : 'Verify'}
                      </button>
                    </div>
                    {settings?.openai_api_key_set && keyStatus === 'idle' && (
                      <div className="flex items-center gap-1.5 mt-2 text-success text-xs">
                        <CheckCircle sx={{ fontSize: 12 }} /> OpenAI API key configured
                      </div>
                    )}
                    {keyStatus === 'ok' && <div className="text-success text-xs mt-2">{keyMessage}</div>}
                    {keyStatus === 'error' && <div className="text-danger text-xs mt-2">{keyMessage}</div>}
                    <div className="text-[11px] text-faint mt-2">
                      Get your key at{' '}
                      <a href="https://platform.openai.com/api-keys" target="_blank" rel="noopener" className="text-accent hover:underline">
                        platform.openai.com
                      </a>
                      {' '}\u2014 enables GPT models
                    </div>
                  </div>
                </div>

                {/* xAI Grok Key */}
                <div className="mt-6 pt-5 border-t border-border">
                  <SectionHeader title="xAI Grok API Key" subtitle="Optional — enables Grok 4.5" />
                  <div className="mt-3">
                    <div className="flex gap-2">
                      <div className="relative flex-1">
                        <input
                          type={showGrokKey ? 'text' : 'password'}
                          value={grokKey}
                          onChange={(e) => { setGrokKey(e.target.value); setGrokStatus('idle') }}
                          onPaste={(e) => {
                            e.preventDefault()
                            const pasted = e.clipboardData.getData('text').trim()
                            if (pasted) { setGrokKey(pasted); setGrokStatus('idle'); clientLog('grok_key_pasted', { keyLength: pasted.length }) }
                          }}
                          placeholder={grokConnected ? '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022' : 'xai-\u2026'}
                          className={`input pr-10 ${grokStatus === 'ok' ? 'border-success focus:border-success' : grokStatus === 'error' ? 'border-danger' : ''}`}
                          onKeyDown={(e) => e.key === 'Enter' && handleVerifyGrokKey()}
                          autoComplete="off"
                          spellCheck={false}
                        />
                        <button
                          onClick={() => { setShowGrokKey(!showGrokKey); clientLog('grok_key_visibility_toggled', { visible: !showGrokKey }) }}
                          className="absolute right-3 top-1/2 -translate-y-1/2 text-faint hover:text-muted"
                        >
                          {showGrokKey ? <VisibilityOff sx={{ fontSize: 14 }} /> : <Visibility sx={{ fontSize: 14 }} />}
                        </button>
                      </div>
                      <button
                        onClick={handleVerifyGrokKey}
                        disabled={grokVerifying || !grokKey.trim()}
                        className="btn-primary px-5 disabled:opacity-40 disabled:cursor-not-allowed"
                      >
                        {grokVerifying ? '\u2026' : 'Verify'}
                      </button>
                    </div>
                    {grokConnected && grokStatus === 'idle' && (
                      <div className="flex items-center gap-1.5 mt-2 text-success text-xs">
                        <CheckCircle sx={{ fontSize: 12 }} /> xAI Grok API key configured
                      </div>
                    )}
                    {grokStatus === 'ok' && <div className="text-success text-xs mt-2">{grokMessage}</div>}
                    {grokStatus === 'error' && <div className="text-danger text-xs mt-2">{grokMessage}</div>}
                    <div className="text-[11px] text-faint mt-2">
                      Get your key at{' '}
                      <a href="https://console.x.ai/team/default/api-keys" target="_blank" rel="noopener" className="text-accent hover:underline"
                         onClick={() => clientLog('grok_console_link_clicked', {})}>
                        console.x.ai
                      </a>
                      {' '}{'\u2014'} enables Grok 4.5 (500K context, reasoning-only)
                    </div>
                  </div>
                </div>

                {false && <div className="mt-6 pt-5 border-t border-border">
                  <SectionHeader title="Google Gemini API Key" subtitle="Enables Gemini 2.5 Pro/Flash" />
                </div>}
              </div>
            )}

            {tab === 'models' && (
              <div className="space-y-5">
                <SectionHeader title="Model Configuration" subtitle="Select which AI model powers SurgicalAI" />

                <Field label="AI Model">
                  <Select value={form.architect_model} onChange={upd('architect_model')} options={models.filter((m) => m.role === 'architect').map((m) => ({ value: m.id, label: `${'$'.repeat(m.cost || 1)} — ${m.name} — ${m.description || ''}` }))} />
                </Field>

                {!form.architect_model.startsWith('claude-') && !form.architect_model.startsWith('gpt-5') && (
                <Field label={`Temperature: ${form.temperature_architect}`}>
                  <input type="range" min="0" max="1" step="0.1" value={form.temperature_architect}
                    onChange={(e) => upd('temperature_architect')(parseFloat(e.target.value))} className="w-full accent-accent" />
                  <div className="flex justify-between text-[10px] text-faint mt-0.5">
                    <span>0 \u2014 precise</span><span>1 \u2014 creative</span>
                  </div>
                </Field>
                )}

                {/* Confidence Threshold slider hidden — not wired to pipeline (uses hardcoded 8/10 gate) */}

                <div className="mt-6 pt-5 border-t border-border">
                  <SectionHeader title="Web Search" subtitle="Claude looks things up live before answering — real-time facts, cited sources" />
                  <label className="flex items-start gap-3 cursor-pointer group mt-3">
                    <input
                      type="checkbox"
                      checked={form.web_search_enabled}
                      onChange={(e) => {
                        const next = e.target.checked
                        upd('web_search_enabled')(next)
                        clientLog('settings_web_search_enabled_toggled', { enabled: next })
                      }}
                      className="mt-0.5 accent-accent"
                    />
                    <div>
                      <div className="text-sm font-medium text-ink">Enable web search in Ask &amp; Plan mode</div>
                      <div className="text-xs text-muted mt-0.5">
                        Claude-only. When on, Claude can search the web and cite sources while answering
                        questions or planning — before any code changes are made. Editing (Edit/Agent mode)
                        has its own separate "Research" checkbox in the message box, since edits are riskier
                        to base on unreviewed live results by default.
                      </div>
                    </div>
                  </label>
                </div>

                <div className="mt-6 pt-5 border-t border-border">
                  <SectionHeader title="Offline Mode" subtitle="Run fully locally via Ollama — no data leaves your machine" />
                  <label className="flex items-start gap-3 cursor-pointer group mt-3">
                    <input type="checkbox" checked={form.ollama_enabled} onChange={(e) => upd('ollama_enabled')(e.target.checked)}
                      className="mt-0.5 accent-accent" />
                    <div>
                      <div className="text-sm font-medium text-ink">Enable offline mode (Qwen2.5-Coder via Ollama)</div>
                      <div className="text-xs text-muted mt-0.5">
                        Used when no cloud API key is configured. Plain chat plus single-file rewrites only —
                        no agent mode / multi-step tasks, since local 7B models aren't reliable at those yet.
                      </div>
                    </div>
                  </label>

                  {form.ollama_enabled && (
                    <div className="space-y-3 mt-4">
                      <Field label="Ollama server URL">
                        <input type="text" value={form.ollama_base_url} onChange={(e) => upd('ollama_base_url')(e.target.value)}
                          placeholder="http://localhost:11434"
                          className="w-full bg-overlay border border-border rounded-lg px-3 py-2 text-sm text-ink" />
                      </Field>
                      <Field label="Model">
                        <input type="text" value={form.ollama_model} onChange={(e) => upd('ollama_model')(e.target.value)}
                          placeholder="qwen2.5-coder:7b"
                          className="w-full bg-overlay border border-border rounded-lg px-3 py-2 text-sm text-ink" />
                      </Field>
                      <div className="text-xs text-faint">
                        Requires Ollama running locally with the model pulled (<code className="font-mono bg-overlay px-1 rounded">ollama pull qwen2.5-coder:7b</code>).
                      </div>
                    </div>
                  )}
                </div>
              </div>
            )}

            {tab === 'workspace' && (
              <div className="space-y-4">
                {settings?.is_hosted ? (
                  <>
                    <SectionHeader title="Workspace" subtitle="Hosted instances don't expose a local filesystem" />
                    <div className="text-sm text-muted">
                      This is a hosted SurgicalAI instance, so there's no local folder for it to browse — connect a GitHub repository instead and SurgicalAI will read and edit files there.
                    </div>
                    <button
                      type="button"
                      onClick={() => setTab('github')}
                      className="btn-primary px-4 py-2 text-sm flex items-center gap-2 w-fit"
                    >
                      <GitHub sx={{ fontSize: 16 }} />
                      Connect GitHub
                    </button>
                  </>
                ) : (
                  <>
                    <SectionHeader title="Workspace" subtitle="Default project folder for the file browser" />
                    <Field label="Default workspace path">
                      <div className="flex gap-2">
                        <input
                          value={form.workspace_path}
                          onChange={(e) => upd('workspace_path')(e.target.value)}
                          placeholder="/home/user/projects/my-app"
                          className="input flex-1"
                        />
                        <button
                          type="button"
                          onClick={handleBrowseDirectory}
                          disabled={browsingDir}
                          className="btn-ghost border border-border px-3 text-sm disabled:opacity-50 flex-shrink-0 flex items-center gap-1.5"
                        >
                          <FolderOpen sx={{ fontSize: 14 }} />
                          {browsingDir ? 'Opening…' : 'Browse…'}
                        </button>
                      </div>
                      <div className="text-[11px] text-faint mt-1">Opens automatically when the app starts</div>
                      <div className="text-[11px] text-faint mt-1">
                        "Browse…" opens a native folder picker on the machine running SurgicalAI's backend — only works when running locally with a display available.
                      </div>
                    </Field>
                  </>
                )}
              </div>
            )}

            {tab === 'editor' && (
              <div className="space-y-5">
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
                      <DarkMode sx={{ fontSize: 14 }} /> Dark
                    </button>
                    <button
                      onClick={() => setTheme('light')}
                      className={`flex items-center gap-2 px-3 py-2 rounded-lg border text-xs font-medium transition ${
                        theme === 'light'
                          ? 'border-accent bg-accent/10 text-accent'
                          : 'border-border bg-surface text-muted hover:text-ink'
                      }`}
                    >
                      <LightMode sx={{ fontSize: 14 }} /> Light
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
                    <div className="text-sm font-medium text-ink">Auto-backup before every surgical change (local files)</div>
                    <div className="text-xs text-muted mt-0.5">When editing files directly on disk from the Surgical panel, saves a timestamped copy to <code className="font-mono text-[10px] bg-overlay px-1 rounded">.surgicalai_backups/</code> before every change</div>
                  </div>
                </label>
                <div className="flex items-start gap-3">
                  <div className="mt-0.5 w-4 h-4 flex-shrink-0" />
                  <div>
                    <div className="text-sm font-medium text-ink">Version history — always on, everywhere</div>
                    <div className="text-xs text-muted mt-0.5">Every applied change to a session file is saved as a restorable checkpoint — not just the last one. Click <span className="font-medium text-ink">History</span> next to any file's changes to browse and restore any previous version.</div>
                  </div>
                </div>
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

                <GitHubAppPanel />

                <div className="text-xs text-muted uppercase tracking-wide pt-2">Legacy: Personal Access Token</div>

                {githubStatus?.connected ? (
                  <div className="space-y-4">
                    <div className="flex items-center gap-3 p-3 rounded-lg border border-green-500/30 bg-green-500/10 flex-wrap">
                      {githubStatus.avatar_url && (
                        <img src={githubStatus.avatar_url} alt="avatar" className="w-9 h-9 rounded-full border border-border flex-shrink-0" />
                      )}
                      <div className="flex-1 min-w-0">
                        <div className="text-sm font-semibold text-green-400">Connected as @{githubStatus.username}</div>
                        <div className="text-xs text-muted">Your repositories are available in the GitHub sidebar tab</div>
                      </div>
                      <button
                        onClick={handleDisconnectGithub}
                        className="text-xs text-red-400 hover:text-red-300 border border-red-500/30 rounded px-3 py-1.5 hover:bg-red-500/10 transition-colors flex-shrink-0"
                      >
                        Disconnect
                      </button>
                    </div>
                    {githubStatusMsg && <div className="text-xs text-muted">{githubStatusMsg}</div>}
                  </div>
                ) : (
                  <div className="space-y-4">
                    <Field label="Personal Access Token">
                      <div className="flex gap-2 max-sm:flex-col">
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
                          className="btn-primary px-4 text-sm disabled:opacity-50 max-sm:w-full"
                        >
                          {githubConnecting ? 'Connecting\u2026' : 'Connect'}
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

            {tab === 'vercel' && (
              <div className="space-y-5">
                <SectionHeader
                  title="Vercel Integration"
                  subtitle="Connect your Vercel account to monitor deployments and read build logs from SurgicalAI"
                />

                {vercelStatus?.connected ? (
                  <div className="flex flex-col gap-3">
                    <div className="flex items-center gap-3 p-3 rounded-lg border border-green-500/30 bg-green-500/10 flex-wrap">
                      {vercelStatus.avatar_url && (
                        <img src={vercelStatus.avatar_url} alt="avatar" className="w-9 h-9 rounded-full border border-border flex-shrink-0" />
                      )}
                      <div className="flex-1 min-w-0">
                        <div className="text-sm font-semibold text-green-400">Connected as {vercelStatus.username || vercelStatus.email}</div>
                        <div className="text-xs text-muted">Your projects are available in the Vercel sidebar tab</div>
                      </div>
                      <button
                        onClick={handleDisconnectVercel}
                        className="text-xs text-red-400 hover:text-red-300 border border-red-500/30 rounded px-3 py-1.5 hover:bg-red-500/10 transition-colors flex-shrink-0"
                      >
                        Disconnect
                      </button>
                    </div>
                    {vercelStatusMsg && <div className="text-xs text-muted">{vercelStatusMsg}</div>}
                  </div>
                ) : (
                  <div className="space-y-4">
                    <Field label="Personal Access Token">
                      <div className="flex gap-2">
                        <input
                          type="password"
                          className="input flex-1 font-mono"
                          placeholder="vercel_pat_\u2026"
                          value={vercelToken}
                          onChange={e => setVercelToken(e.target.value)}
                          onKeyDown={(e) => e.key === 'Enter' && handleConnectVercel()}
                        />
                        <button
                          onClick={handleConnectVercel}
                          disabled={vercelConnecting || !vercelToken.trim()}
                          className="btn-primary px-4 text-sm disabled:opacity-50"
                        >
                          {vercelConnecting ? 'Connecting\u2026' : 'Connect'}
                        </button>
                      </div>
                    </Field>
                    {vercelStatusMsg && (
                      <div className={`text-xs px-3 py-2 rounded ${vercelStatusMsg.includes('success') ? 'text-green-400 bg-green-500/10' : 'text-red-400 bg-red-500/10'}`}>
                        {vercelStatusMsg}
                      </div>
                    )}
                    <div className="text-xs text-muted space-y-1 p-3 rounded-lg bg-surface border border-border">
                      <div className="font-medium text-ink mb-2">How to get a token:</div>
                      <div>1. Go to <a href="https://vercel.com/account/tokens" target="_blank" rel="noreferrer" className="text-blue-400 hover:underline">vercel.com/account/tokens</a></div>
                      <div>2. Click <strong className="text-ink">Create Token</strong></div>
                      <div>3. Give it a name and set scope to your team</div>
                      <div>4. Copy the token and paste it above — creating a new token does NOT deactivate existing ones</div>
                    </div>
                  </div>
                )}
              </div>
            )}

            {tab === 'railway' && (
              <div className="space-y-5">
                <SectionHeader
                  title="Railway Integration"
                  subtitle="Connect your Railway account to monitor services and deployments from SurgicalAI"
                />

                {railwayStatus?.connected ? (
                  <div className="flex flex-col gap-3">
                    <div className="flex items-center gap-3 p-3 rounded-lg border border-green-500/30 bg-green-500/10 flex-wrap">
                      <div className="w-9 h-9 rounded-full bg-red-600/20 border border-red-500/30 flex items-center justify-center flex-shrink-0 text-red-400 font-bold text-sm">R</div>
                      <div className="flex-1 min-w-0">
                        <div className="text-sm font-semibold text-green-400">Connected as {railwayStatus.name || railwayStatus.email}</div>
                        <div className="text-xs text-muted">Your projects are available in the Railway sidebar tab</div>
                      </div>
                      <button
                        onClick={handleDisconnectRailway}
                        className="text-xs text-red-400 hover:text-red-300 border border-red-500/30 rounded px-3 py-1.5 hover:bg-red-500/10 transition-colors flex-shrink-0"
                      >
                        Disconnect
                      </button>
                    </div>
                    {railwayStatusMsg && <div className="text-xs text-muted">{railwayStatusMsg}</div>}
                  </div>
                ) : (
                  <div className="space-y-4">
                    <Field label="Personal Access Token">
                      <div className="flex gap-2">
                        <input
                          type="password"
                          className="input flex-1 font-mono"
                          placeholder="railway_token_…"
                          value={railwayToken}
                          onChange={e => setRailwayToken(e.target.value)}
                          onKeyDown={(e) => e.key === 'Enter' && handleConnectRailway()}
                        />
                        <button
                          onClick={handleConnectRailway}
                          disabled={railwayConnecting || !railwayToken.trim()}
                          className="btn-primary px-4 text-sm disabled:opacity-50"
                        >
                          {railwayConnecting ? 'Connecting…' : 'Connect'}
                        </button>
                      </div>
                    </Field>
                    {railwayStatusMsg && (
                      <div className={`text-xs px-3 py-2 rounded ${railwayStatusMsg.includes('success') ? 'text-green-400 bg-green-500/10' : 'text-red-400 bg-red-500/10'}`}>
                        {railwayStatusMsg}
                      </div>
                    )}
                    <div className="text-xs text-muted space-y-1 p-3 rounded-lg bg-surface border border-border">
                      <div className="font-medium text-ink mb-2">How to get a token:</div>
                      <div>1. Go to <a href="https://railway.com/account/tokens" target="_blank" rel="noreferrer" className="text-blue-400 hover:underline">railway.com/account/tokens</a></div>
                      <div>2. Click <strong className="text-ink">New Token</strong></div>
                      <div>3. Give it a name (e.g. "SurgicalAI")</div>
                      <div>4. Copy the token and paste it above</div>
                    </div>
                  </div>
                )}
              </div>
            )}

            {tab === 'debug' && (
              <DebugLogsPanel />
            )}

            {tab === 'security' && (
              <SecurityPanel
                pwCurrent={pwCurrent} setPwCurrent={setPwCurrent}
                pwNew={pwNew} setPwNew={setPwNew}
                pwConfirm={pwConfirm} setPwConfirm={setPwConfirm}
                showPwCurrent={showPwCurrent} setShowPwCurrent={setShowPwCurrent}
                showPwNew={showPwNew} setShowPwNew={setShowPwNew}
                showPwConfirm={showPwConfirm} setShowPwConfirm={setShowPwConfirm}
                pwResult={pwResult} setPwResult={setPwResult}
                pwSaving={pwSaving}
                onChangePassword={handleChangePassword}
                pwStrength={pwStrength}
                pwStrengthLabel={pwStrengthLabel}
                pwStrengthColor={pwStrengthColor}
                pwStrengthText={pwStrengthText}
              />
            )}
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-border flex-shrink-0">
          <button onClick={() => setSettingsOpen(false)} className="btn-ghost border border-border px-5 py-2 text-sm">Cancel</button>
          {tab !== 'users' && tab !== 'security' && tab !== 'debug' && <button onClick={handleSave} className="btn-primary px-6">Save Settings</button>}
        </div>
      </div>
    </div>
  )
}

interface SecurityPanelProps {
  pwCurrent: string; setPwCurrent: (v: string) => void
  pwNew: string; setPwNew: (v: string) => void
  pwConfirm: string; setPwConfirm: (v: string) => void
  showPwCurrent: boolean; setShowPwCurrent: (fn: (v: boolean) => boolean) => void
  showPwNew: boolean; setShowPwNew: (fn: (v: boolean) => boolean) => void
  showPwConfirm: boolean; setShowPwConfirm: (fn: (v: boolean) => boolean) => void
  pwResult: { ok: boolean; msg: string } | null; setPwResult: (v: { ok: boolean; msg: string } | null) => void
  pwSaving: boolean
  onChangePassword: () => void
  pwStrength: (pw: string) => number
  pwStrengthLabel: string[]
  pwStrengthColor: string[]
  pwStrengthText: string[]
}

function SecurityPanel(props: SecurityPanelProps) {
  const {
    pwCurrent, setPwCurrent, pwNew, setPwNew, pwConfirm, setPwConfirm,
    showPwCurrent, setShowPwCurrent, showPwNew, setShowPwNew,
    showPwConfirm, setShowPwConfirm, pwResult, setPwResult,
    pwSaving, onChangePassword, pwStrength, pwStrengthLabel, pwStrengthColor, pwStrengthText,
  } = props

  const pwHas8     = pwNew.length >= 8
  const pwHasUpper = /[A-Z]/.test(pwNew)
  const pwHasLower = /[a-z]/.test(pwNew)
  const pwHasDigit = /[0-9]/.test(pwNew)

  const s = pwNew ? pwStrength(pwNew) : 0
  const pwResultCls = pwResult
    ? (pwResult.ok ? 'bg-green-500/10 border border-green-500/30 text-green-400' : 'bg-red-500/10 border border-red-500/30 text-red-400')
    : ''

  return (
    <div className="space-y-6 max-w-sm">
      <SectionHeader title="Change Password" subtitle="Enter your current password to set a new one" />

      <div>
        <div className="text-xs text-muted mb-1.5 font-medium">Current Password</div>
        <div className="relative">
          <input
            type={showPwCurrent ? 'text' : 'password'}
            className="input pr-10 w-full"
            placeholder="Your current password"
            value={pwCurrent}
            onChange={(e) => { setPwCurrent(e.target.value); setPwResult(null) }}
            autoComplete="current-password"
          />
          <button type="button" onClick={() => setShowPwCurrent(v => !v)} className="absolute right-3 top-1/2 -translate-y-1/2 text-muted hover:text-ink">
            {showPwCurrent ? <VisibilityOff sx={{ fontSize: 14 }} /> : <Visibility sx={{ fontSize: 14 }} />}
          </button>
        </div>
      </div>

      <div>
        <div className="text-xs text-muted mb-1.5 font-medium">New Password</div>
        <div className="relative">
          <input
            type={showPwNew ? 'text' : 'password'}
            className="input pr-10 w-full"
            placeholder="At least 8 chars, upper, lower, digit"
            value={pwNew}
            onChange={(e) => { setPwNew(e.target.value); setPwResult(null) }}
            autoComplete="new-password"
          />
          <button type="button" onClick={() => setShowPwNew(v => !v)} className="absolute right-3 top-1/2 -translate-y-1/2 text-muted hover:text-ink">
            {showPwNew ? <VisibilityOff sx={{ fontSize: 14 }} /> : <Visibility sx={{ fontSize: 14 }} />}
          </button>
        </div>
        {pwNew && (
          <div className="mt-2 space-y-1">
            <div className="flex gap-1">
              {[1,2,3,4].map(i => (
                <div key={i} className={`h-1 flex-1 rounded-full transition-colors ${i <= s ? pwStrengthColor[s] : 'bg-border'}`} />
              ))}
            </div>
            <div className={`text-[11px] font-medium ${pwStrengthText[s]}`}>
              {pwStrengthLabel[s]}
              {s < 3 && <span className="text-faint ml-1">&mdash; add uppercase, numbers, or symbols</span>}
            </div>
          </div>
        )}
      </div>

      <div>
        <div className="text-xs text-muted mb-1.5 font-medium">Confirm New Password</div>
        <div className="relative">
          <input
            type={showPwConfirm ? 'text' : 'password'}
            className="input pr-10 w-full"
            placeholder="Repeat your new password"
            value={pwConfirm}
            onChange={(e) => { setPwConfirm(e.target.value); setPwResult(null) }}
            autoComplete="new-password"
            onKeyDown={(e) => e.key === 'Enter' && onChangePassword()}
          />
          <button type="button" onClick={() => setShowPwConfirm(v => !v)} className="absolute right-3 top-1/2 -translate-y-1/2 text-muted hover:text-ink">
            {showPwConfirm ? <VisibilityOff sx={{ fontSize: 14 }} /> : <Visibility sx={{ fontSize: 14 }} />}
          </button>
        </div>
        {pwConfirm && pwNew && (
          <div className={`text-[11px] mt-1 font-medium ${pwConfirm === pwNew ? 'text-green-400' : 'text-red-400'}`}>
            {pwConfirm === pwNew ? '\u2713 Passwords match' : '\u2717 Passwords do not match'}
          </div>
        )}
      </div>

      {pwResult && (
        <div className={`flex items-start gap-2 px-3 py-2.5 rounded-lg text-xs ${pwResultCls}`}>
          {pwResult.ok ? <CheckCircle sx={{ fontSize: 13 }} className="mt-px shrink-0" /> : <ErrorOutline sx={{ fontSize: 13 }} className="mt-px shrink-0" />}
          {pwResult.msg}
        </div>
      )}

      <button onClick={onChangePassword} disabled={pwSaving || !pwCurrent || !pwNew || !pwConfirm} className="btn-primary w-full disabled:opacity-50">
        {pwSaving ? 'Updating\u2026' : 'Update Password'}
      </button>

      <div className="text-[11px] text-faint space-y-0.5 p-3 rounded-lg bg-surface border border-border">
        <div className="font-medium text-muted mb-1">Requirements</div>
        <div className={`flex items-center gap-1.5 ${pwHas8 ? 'text-green-400' : ''}`}>
          {pwHas8 ? '\u2713' : '\u00B7'} At least 8 characters
        </div>
        <div className={`flex items-center gap-1.5 ${pwHasUpper ? 'text-green-400' : ''}`}>
          {pwHasUpper ? '\u2713' : '\u00B7'} One uppercase letter
        </div>
        <div className={`flex items-center gap-1.5 ${pwHasLower ? 'text-green-400' : ''}`}>
          {pwHasLower ? '\u2713' : '\u00B7'} One lowercase letter
        </div>
        <div className={`flex items-center gap-1.5 ${pwHasDigit ? 'text-green-400' : ''}`}>
          {pwHasDigit ? '\u2713' : '\u00B7'} One number
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


// ── Debug Logs Panel ──────────────────────────────────────────────────────────

interface LogEvent {
  ts: string
  event: string
  session_id?: string
  [key: string]: any
}

const EVENT_COLORS: Record<string, string> = {
  file_context_built:        'text-blue-400',
  search_requested:          'text-yellow-400',
  search_results_returned:   'text-green-400',
  edit_blocks_collected:     'text-purple-400',
  snippet_apply_failed:      'text-red-400',
  correction_prompt_sent:    'text-orange-400',
  correction_response:       'text-cyan-400',
}

function DebugLogsPanel() {
  const [events, setEvents] = useState<LogEvent[]>([])
  const [loading, setLoading] = useState(false)
  const [expanded, setExpanded] = useState<number | null>(null)
  const [filter, setFilter] = useState<string>('all')
  const [autoRefresh, setAutoRefresh] = useState(false)
  const [sessionFilter, setSessionFilter] = useState('')
  const [userFilter, setUserFilter] = useState('')
  const [totalCount, setTotalCount] = useState(0)
  const [filteredCount, setFilteredCount] = useState(0)
  const [copied, setCopied] = useState(false)
  const { token } = useAuthStore()
  const { activeSessions } = useAppStore()

  useEffect(() => {
    if (activeSessions && !sessionFilter) {
      setSessionFilter(activeSessions)
    }
  }, [activeSessions])

  const BASE_URL = (import.meta.env.VITE_API_URL ?? '') + '/api'

  const load = async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams()
      if (sessionFilter.trim()) params.set('session_id', sessionFilter.trim())
      if (userFilter.trim()) params.set('user_id', userFilter.trim())
      const res = await fetch(`${BASE_URL}/debug/pipeline-log?${params}`, {
        headers: { Authorization: `Bearer ${token}` },
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      const all: LogEvent[] = data.events || []
      setTotalCount(all.length)
      const filtered = filter === 'all' ? all : all.filter(e => e.event === filter)
      setFilteredCount(filtered.length)
      setEvents(filtered)
    } catch (e: any) {
      toast.error('Failed to load debug logs', e.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [filter, sessionFilter, userFilter])

  useEffect(() => {
    if (!autoRefresh) return
    const id = setInterval(load, 5000)
    return () => clearInterval(id)
  }, [autoRefresh, filter, sessionFilter, userFilter])

  const handleClear = async () => {
    if (!confirm('Clear all pipeline debug logs?')) return
    try {
      await fetch(`${BASE_URL}/debug/pipeline-log`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      })
      setEvents([])
      setTotalCount(0)
      setFilteredCount(0)
      toast.success('Debug logs cleared')
    } catch (e: any) {
      toast.error('Failed to clear logs', e.message)
    }
  }

  const handleDownload = () => {
    const params = new URLSearchParams()
    if (token) params.set('token', token)
    if (sessionFilter.trim()) params.set('session_id', sessionFilter.trim())
    if (userFilter.trim()) params.set('user_id', userFilter.trim())
    window.open(`${BASE_URL}/debug/pipeline-log/download?${params}`, '_blank')
  }

  const handleCopy = async () => {
    const text = events.map(e => JSON.stringify(e)).join('\n')
    await navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const eventTypes = ['all', ...Array.from(new Set(events.map(e => e.event)))]

  return (
    <div className="flex flex-col gap-3 h-full">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div>
          <div className="text-sm font-bold text-ink">Pipeline Debug Logs</div>
          <div className="text-xs text-muted">{filteredCount} of {totalCount} events</div>
        </div>
        <div className="flex gap-1.5 flex-wrap">
          <button onClick={load} disabled={loading} className="btn-ghost border border-border text-xs px-2.5 py-1.5">
            {loading ? '\u2026' : '\u21bb Refresh'}
          </button>
          <button
            onClick={() => setAutoRefresh(v => !v)}
            className={`text-xs px-2.5 py-1.5 rounded border transition-colors ${
              autoRefresh ? 'bg-accent/20 border-accent text-accent' : 'border-border text-muted hover:text-ink'
            }`}
          >
            {autoRefresh ? 'Auto \u25a0' : 'Auto \u25b6'}
          </button>
          <button onClick={handleCopy} className="btn-ghost border border-border text-xs px-2.5 py-1.5">
            {copied ? '\u2713 Copied' : 'Copy'}
          </button>
          <button onClick={handleDownload} className="btn-ghost border border-border text-xs px-2.5 py-1.5">
            \u2193 Download
          </button>
          <button onClick={handleClear} className="text-xs px-2.5 py-1.5 rounded border border-red-500/30 text-red-400 hover:bg-red-500/10 transition-colors">
            Clear
          </button>
        </div>
      </div>

      <div className="flex gap-2 flex-wrap">
        <input
          className="input flex-1 min-w-0 text-xs py-1"
          placeholder="Filter by session ID\u2026"
          value={sessionFilter}
          onChange={e => setSessionFilter(e.target.value)}
        />
        <input
          className="input flex-1 min-w-0 text-xs py-1"
          placeholder="Filter by user ID\u2026"
          value={userFilter}
          onChange={e => setUserFilter(e.target.value)}
        />
      </div>

      <div className="flex gap-1.5 flex-wrap">
        {eventTypes.map(t => (
          <button
            key={t}
            onClick={() => setFilter(t)}
            className={`text-[10px] px-2 py-0.5 rounded-full border transition-colors ${
              filter === t
                ? 'bg-accent/20 border-accent text-accent'
                : 'border-border text-faint hover:text-ink'
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto space-y-1 min-h-0">
        {events.length === 0 && !loading && (
          <div className="text-xs text-faint text-center py-8">
            No events yet. Run a chat prompt to see pipeline activity.
          </div>
        )}
        {events.map((ev, i) => (
          <div
            key={i}
            className="rounded-lg border border-border bg-surface-alt/30 overflow-hidden"
          >
            <button
              onClick={() => setExpanded(expanded === i ? null : i)}
              className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-overlay/50 transition-colors"
            >
              <span className={`text-[10px] font-mono font-semibold flex-1 truncate ${EVENT_COLORS[ev.event] || 'text-ink'}`}>
                {ev.event}
              </span>
              <span className="text-[10px] text-faint flex-shrink-0">
                {ev.ts ? new Date(ev.ts).toLocaleTimeString() : ''}
              </span>
              <span className="text-[10px] text-faint flex-shrink-0">{expanded === i ? '\u25b2' : '\u25bc'}</span>
            </button>
            {expanded === i && (
              <div className="px-3 pb-3 border-t border-border/40">
                <pre className="text-[10px] text-muted font-mono whitespace-pre-wrap break-all mt-2 max-h-64 overflow-y-auto">
                  {JSON.stringify(ev, null, 2)}
                </pre>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
