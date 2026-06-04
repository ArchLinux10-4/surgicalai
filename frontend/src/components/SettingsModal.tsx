import React, { useState, useEffect } from 'react'
import { useAppStore } from '../stores/appStore'
import { api } from '../api/client'
import { toast } from '../lib/toast'
import { AdminUsersPanel } from './AdminUsersPanel'
import { useAuthStore } from '../stores/authStore'
import { useThemeStore } from '../stores/themeStore'
import { BugReport, CheckCircle, Close, Code, DarkMode, ErrorOutline, FolderOpen, GitHub, Group, LightMode, Lock, Memory, OpenInNew, Psychology, Tune, Visibility, VisibilityOff, VpnKey } from '@mui/icons-material';

type Tab = 'api' | 'models' | 'workspace' | 'editor' | 'users' | 'github' | 'security' | 'debug'

const TABS: { id: Tab; icon: React.ReactNode; label: string }[] = [
  { id: 'api',       icon: <VpnKey sx={{ fontSize: 14 }} />,       label: 'API Keys' },
  { id: 'models',    icon: <Psychology sx={{ fontSize: 14 }} />,      label: 'Models' },
  { id: 'github',    icon: <GitHub sx={{ fontSize: 14 }} />,     label: 'GitHub' },
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
  const [githubPat, setGithubPat] = useState('')
  const [showGithubPat, setShowGithubPat] = useState(false)
  const [githubConnecting, setGithubConnecting] = useState(false)
  const [githubStatus, setGithubStatus] = useState<any>(null)
  const [githubStatusMsg, setGithubStatusMsg] = useState('')
  const [models, setModels] = useState<any[]>([])

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

  // Password strength — 0-4 based on criteria met
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
          {/* Left tabs — sidebar on desktop, horizontal scroll strip on mobile */}
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
                {/* OpenAI hidden — app is optimised for Claude API only */}
              {false && <>
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
                        {showKey ? <VisibilityOff sx={{ fontSize: 14 }} /> : <Visibility sx={{ fontSize: 14 }} />}
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
                      <CheckCircle sx={{ fontSize: 12 }} /> API key configured
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
              </>}

                {/* ── Anthropic / Claude Key ── */}
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
                          {showAnthropicKey ? <VisibilityOff sx={{ fontSize: 14 }} /> : <Visibility sx={{ fontSize: 14 }} />}
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
                      {' '}— enables Claude Sonnet 4 & Opus 4 with visible thinking
                    </div>
                  </div>
                </div>

                {/* Google Gemini hidden — app is optimised for Claude API only */}
                {false && <div className="mt-6 pt-5 border-t border-border">
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
                          {showGeminiKey ? <VisibilityOff sx={{ fontSize: 14 }} /> : <Visibility sx={{ fontSize: 14 }} />}
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
                        <CheckCircle sx={{ fontSize: 12 }} /> Gemini API key configured
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
                </div>}
              </div>
            )}

            {tab === 'models' && (
              <div className="space-y-5">
                <SectionHeader title="Model Configuration" subtitle="Select which Claude model powers SurgicalAI" />

                <Field label="Claude Model">
                  <Select value={form.architect_model} onChange={upd('architect_model')} options={models.filter((m) => m.role === 'architect').map((m) => ({ value: m.id, label: `${m.name} — ${m.description}` }))} />
                </Field>

                {/* Surgeon model hidden — natural pipeline uses a single Claude model */}
                {false && (
                <Field label="Surgeon Model (code writing)">
                  <Select value={form.surgeon_model} onChange={upd('surgeon_model')} options={models.filter((m) => m.role === 'surgeon' || m.role === 'fast').map((m) => ({ value: m.id, label: `${m.name} — ${m.description}` }))} />
                </Field>
                )}

                {/* Temperature hidden for Claude models: extended thinking requires
                    temperature=1, so the control is inert on Claude. Shown only if a
                    non-Claude model is ever re-enabled. */}
                {!form.architect_model.startsWith('claude-') && (
                <Field label={`Temperature: ${form.temperature_architect}`}>
                  <input type="range" min="0" max="1" step="0.1" value={form.temperature_architect}
                    onChange={(e) => upd('temperature_architect')(parseFloat(e.target.value))} className="w-full accent-accent" />
                  <div className="flex justify-between text-[10px] text-faint mt-0.5">
                    <span>0 — precise</span><span>1 — creative</span>
                  </div>
                </Field>
                )}

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
                    <div className="text-sm font-medium text-ink">Auto-backup before every surgical change</div>
                    <div className="text-xs text-muted mt-0.5">Saves to <code className="font-mono text-[10px] bg-overlay px-1 rounded">.surgicalai_backups/</code> — always reversible</div>
                  </div>
                </label>
              </div>
            )}

            {/* Local AI tab hidden — app is optimised for Claude API only */}
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
                    {githubStatusMsg && (
                      <div className="text-xs text-muted">{githubStatusMsg}</div>
                    )}
                  </div>
                ) : (
                  /* Not connected state */
                  <div className="space-y-4">
                    <Field label="Personal Access Token (Classic)">
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

// ── Security / Change Password panel ─────────────────────────────────────────
// Extracted into its own component to avoid esbuild 0.25.x JSX nesting bugs
// (IIFE-in-JSX and regex-in-template-literals both misparse at this nesting depth)
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

      {/* Current password */}
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

      {/* New password */}
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

      {/* Confirm password */}
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

      {/* Result banner */}
      {pwResult && (
        <div className={`flex items-start gap-2 px-3 py-2.5 rounded-lg text-xs ${pwResultCls}`}>
          {pwResult.ok ? <CheckCircle sx={{ fontSize: 13 }} className="mt-px shrink-0" /> : <ErrorOutline sx={{ fontSize: 13 }} className="mt-px shrink-0" />}
          {pwResult.msg}
        </div>
      )}

      {/* Submit */}
      <button onClick={onChangePassword} disabled={pwSaving || !pwCurrent || !pwNew || !pwConfirm} className="btn-primary w-full disabled:opacity-50">
        {pwSaving ? 'Updating\u2026' : 'Update Password'}
      </button>

      {/* Requirements callout */}
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
// Admin-only tab: live view, download, and clear pipeline debug logs.

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
  const { token } = useAuthStore()

  const BASE = import.meta.env.VITE_API_URL || ''

  const fetchLogs = async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams({ last: '500' })
      if (sessionFilter.trim()) params.set('session_id', sessionFilter.trim())
      if (userFilter.trim()) params.set('user_id', userFilter.trim())
      const res = await fetch(`${BASE}/api/debug/pipeline-log?${params}`, {
        headers: { Authorization: `Bearer ${token}` },
      })
      if (!res.ok) throw new Error(`${res.status}`)
      const data = await res.json()
      setEvents((data.events || []).slice().reverse()) // newest first
      setTotalCount(data.total ?? 0)
      setFilteredCount(data.filtered ?? 0)
    } catch (e: any) {
      toast.error('Could not load debug logs', e.message)
    } finally {
      setLoading(false)
    }
  }

  const clearLogs = async () => {
    try {
      await fetch(`${BASE}/api/debug/pipeline-log`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      })
      setEvents([])
      toast.success('Debug log cleared')
    } catch (e: any) {
      toast.error('Clear failed', e.message)
    }
  }

  const downloadLogs = () => {
    const params = new URLSearchParams()
    if (sessionFilter.trim()) params.set('session_id', sessionFilter.trim())
    if (userFilter.trim()) params.set('user_id', userFilter.trim())
    const qs = params.toString()
    window.open(`${BASE}/api/debug/pipeline-log/download${qs ? '?' + qs : ''}`, '_blank')
  }

  useEffect(() => { fetchLogs() }, [])

  useEffect(() => {
    if (!autoRefresh) return
    const id = setInterval(fetchLogs, 5000)
    return () => clearInterval(id)
  }, [autoRefresh])

  const eventTypes = ['all', ...Array.from(new Set(events.map(e => e.event)))]
  const visible = filter === 'all' ? events : events.filter(e => e.event === filter)

  return (
    <div className="flex flex-col h-full gap-4">
      {/* Filter inputs */}
      <div className="flex gap-2 flex-wrap">
        <input
          type="text" value={sessionFilter} onChange={e => setSessionFilter(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && fetchLogs()}
          placeholder="Filter by session ID…"
          className="flex-1 min-w-[180px] bg-overlay border border-border rounded-lg px-3 py-1.5 text-xs text-ink placeholder:text-faint focus:outline-none focus:border-accent"
        />
        <input
          type="text" value={userFilter} onChange={e => setUserFilter(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && fetchLogs()}
          placeholder="Filter by user ID…"
          className="flex-1 min-w-[140px] bg-overlay border border-border rounded-lg px-3 py-1.5 text-xs text-ink placeholder:text-faint focus:outline-none focus:border-accent"
        />
        <button onClick={fetchLogs} disabled={loading}
          className="btn-primary px-3 py-1.5 text-xs rounded-lg disabled:opacity-50">
          {loading ? '…' : 'Search'}
        </button>
      </div>

      {/* Header row */}
      <div className="flex items-center gap-2 flex-wrap">
        <button onClick={fetchLogs} disabled={loading}
          className="btn-ghost border border-border px-3 py-1.5 text-xs rounded-lg disabled:opacity-50">
          {loading ? 'Loading…' : '↻ Refresh'}
        </button>
        <button onClick={() => setAutoRefresh(v => !v)}
          className={`border px-3 py-1.5 text-xs rounded-lg transition-colors ${autoRefresh ? 'bg-accent/20 border-accent text-accent' : 'btn-ghost border-border'}`}>
          {autoRefresh ? '● Live (5s)' : 'Auto-refresh off'}
        </button>
        <button onClick={downloadLogs}
          className="btn-ghost border border-border px-3 py-1.5 text-xs rounded-lg">
          ⬇ Download JSONL
        </button>
        <button onClick={clearLogs}
          className="btn-ghost border border-red-500/40 text-red-400 px-3 py-1.5 text-xs rounded-lg hover:bg-red-500/10">
          🗑 Clear
        </button>
        <span className="text-xs text-muted ml-auto">
          {totalCount > 0 && <span className="text-faint mr-1">{filteredCount}/{totalCount} total</span>}
          {events.length} shown
        </span>
      </div>

      {/* Filter chips */}
      <div className="flex gap-1.5 flex-wrap">
        {eventTypes.map(t => (
          <button key={t} onClick={() => setFilter(t)}
            className={`px-2.5 py-0.5 rounded-full text-[11px] font-medium border transition-colors ${
              filter === t
                ? 'bg-accent text-white border-accent'
                : 'border-border text-muted hover:text-ink hover:border-ink/30'
            }`}>
            {t}
          </button>
        ))}
      </div>

      {/* Log list */}
      <div className="flex-1 overflow-y-auto flex flex-col gap-1.5 min-h-0 font-mono text-xs">
        {visible.length === 0 && (
          <div className="text-muted text-center py-12">
            {loading ? 'Loading…' : 'No log events yet. Run a pipeline edit to see debug output here.'}
          </div>
        )}
        {visible.map((ev, i) => {
          const color = EVENT_COLORS[ev.event] || 'text-ink'
          const isOpen = expanded === i
          const { ts, event, ...rest } = ev
          return (
            <div key={i}
              onClick={() => setExpanded(isOpen ? null : i)}
              className="bg-overlay border border-edge rounded-lg px-3 py-2 cursor-pointer hover:border-accent/40 transition-colors">
              <div className="flex items-center gap-2">
                <span className="text-faint text-[10px] shrink-0">{new Date(ts).toLocaleTimeString()}</span>
                <span className={`font-semibold shrink-0 ${color}`}>{event}</span>
                {ev.session_id && (
                  <span className="text-faint text-[10px] truncate">{ev.session_id.slice(0, 8)}…</span>
                )}
                <span className="ml-auto text-faint text-[10px]">{isOpen ? '▲' : '▼'}</span>
              </div>
              {isOpen && (
                <pre className="mt-2 text-[10px] text-ink/80 whitespace-pre-wrap break-all max-h-64 overflow-y-auto bg-surface rounded p-2 border border-edge">
                  {JSON.stringify(rest, null, 2)}
                </pre>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
