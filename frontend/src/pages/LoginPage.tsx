import React, { useState, useEffect } from 'react'
import { useAuth } from '../contexts/AuthContext'
import { useLogo } from '../hooks/useLogo'
import { useTheme } from '../contexts/ThemeContext'
import { fetchAppSettings } from '../lib/data'
import LightModeRounded from '@mui/icons-material/LightModeRounded'
import DarkModeRounded from '@mui/icons-material/DarkModeRounded'
import VpnKeyRounded from '@mui/icons-material/VpnKeyRounded'
import ErrorOutlineRounded from '@mui/icons-material/ErrorOutlineRounded'
import ArrowForwardRounded from '@mui/icons-material/ArrowForwardRounded'
import ArrowBackRounded from '@mui/icons-material/ArrowBackRounded'
import AutorenewRounded from '@mui/icons-material/AutorenewRounded'
import LockRounded from '@mui/icons-material/LockRounded'
import BoltRounded from '@mui/icons-material/BoltRounded'
import InsightsRounded from '@mui/icons-material/InsightsRounded'
import Inventory2Rounded from '@mui/icons-material/Inventory2Rounded'
import TimerRounded from '@mui/icons-material/TimerRounded'
import TaskAltRounded from '@mui/icons-material/TaskAltRounded'

/* ── Keyframes injected once ── */
const AURORA_KF_ID = 'aurora-login-kf'
const AURORA_CSS = `
@keyframes alp-drift1{0%{transform:translate(0,0) scale(1)}33%{transform:translate(14px,10px) scale(1.05)}66%{transform:translate(8px,20px) scale(0.97)}100%{transform:translate(0,0) scale(1)}}
@keyframes alp-drift2{0%{transform:translate(0,0) scale(1)}40%{transform:translate(-12px,-16px) scale(1.08)}70%{transform:translate(6px,-8px) scale(0.95)}100%{transform:translate(0,0) scale(1)}}
@keyframes alp-drift3{0%{transform:translate(0,0) scale(1);opacity:0.25}50%{transform:translate(-10px,12px) scale(1.1);opacity:0.35}100%{transform:translate(0,0) scale(1);opacity:0.25}}
@keyframes alp-pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(1.5)}}
@keyframes alp-scan{0%{transform:translateY(-100%);opacity:0}10%{opacity:1}90%{opacity:1}100%{transform:translateY(200%);opacity:0}}
@keyframes alp-fadeUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}
@keyframes alp-fadeIn{from{opacity:0}to{opacity:1}}
@keyframes alp-cardGlow{0%,100%{box-shadow:0 0 0 0 rgba(14,165,233,0);border-color:rgba(255,255,255,0.08)}50%{box-shadow:0 0 18px 0 rgba(14,165,233,0.15);border-color:rgba(14,165,233,0.28)}}
@keyframes alp-shimmer{0%{background-position:-200% center}100%{background-position:200% center}}
@keyframes alp-logoGlow{0%,100%{box-shadow:0 0 0 0 rgba(14,165,233,0)}50%{box-shadow:0 0 14px 3px rgba(14,165,233,0.35)}}
`

function useInjectAuroraKeyframes() {
  useEffect(() => {
    if (document.getElementById(AURORA_KF_ID)) return
    const el = document.createElement('style')
    el.id = AURORA_KF_ID
    el.textContent = AURORA_CSS
    document.head.appendChild(el)
  }, [])
}

function useCountUp(target: number, suffix: string, decimals: number, delay: number, duration: number) {
  const [value, setValue] = useState('0' + suffix)
  useEffect(() => {
    const t = setTimeout(() => {
      const t0 = performance.now()
      function step(now: number) {
        const p = Math.min((now - t0) / duration, 1)
        const ease = 1 - Math.pow(1 - p, 3)
        const v = target * ease
        setValue(decimals > 0 ? v.toFixed(1) + suffix : Math.round(v) + suffix)
        if (p < 1) requestAnimationFrame(step)
      }
      requestAnimationFrame(step)
    }, delay)
    return () => clearTimeout(t)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  return value
}

/* ── Shared design tokens (moved inside component for theme reactivity) ── */


/* ── Sub-components ── */

const LiveDot = ({ size = 6 }: { size?: number }) => (
  <span style={{ width: size, height: size, background: '#4ade80', borderRadius: '50%', display: 'inline-block', flexShrink: 0, animation: 'alp-pulse 2s infinite' }} />
)

const GridOverlay = ({ size = 28, color = 'rgba(99,179,237,0.06)' }: { size?: number; color?: string }) => (
  <div style={{
    position: 'absolute', inset: 0, pointerEvents: 'none',
    backgroundImage: `linear-gradient(${color} 1px, transparent 1px), linear-gradient(90deg, ${color} 1px, transparent 1px)`,
    backgroundSize: `${size}px ${size}px`, overflow: 'hidden',
    transition: 'background-image 0.35s',
  }} />
)

const Blob = ({ w, h, color, top, left, bottom, right, opacity, anim, blur = 70 }: {
  w: number; h: number; color: string; top?: number | string; left?: number | string;
  bottom?: number | string; right?: number | string; opacity: number; anim: string; blur?: number
}) => (
  <div style={{
    position: 'absolute', width: w, height: h, borderRadius: '50%',
    filter: `blur(${blur}px)`, background: color,
    top, left, bottom, right, opacity,
    animation: anim,
    pointerEvents: 'none',
  }} />
)

const StatPill = ({
  val, label, icon,
  valColor = '#f0f8ff',
  labelColor = 'rgba(180,210,255,0.5)',
  bg = 'rgba(14,165,233,0.08)',
  border = '1px solid rgba(14,165,233,0.18)',
}: {
  val: string; label: string; icon?: React.ReactNode;
  valColor?: string; labelColor?: string; bg?: string; border?: string;
}) => (
  <div style={{
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    gap: 2,
    background: bg,
    border: border,
    borderRadius: 8,
    padding: '10px 12px',
    minWidth: 64,
    flex: 1,
    textAlign: 'center',
    transition: 'background 0.35s, border-color 0.35s',
  }}>
    {icon && (
      <span style={{ fontSize: 14, marginBottom: 2, lineHeight: 1 }}>{icon}</span>
    )}
    <div style={{ fontSize: 15, fontWeight: 700, color: valColor, lineHeight: 1.1, transition: 'color 0.35s' }}>{val}</div>
    <div style={{ fontSize: 9, color: labelColor, marginTop: 1, transition: 'color 0.35s' }}>{label}</div>
  </div>
)

export const LoginPage: React.FC = () => {
  const { signIn, signInWithSSO } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const { logoUrl } = useLogo()
  const { isDark, toggleTheme } = useTheme()

  /* ── Left panel design tokens — reactive to theme ── */
  const LP = isDark ? {
    bg: 'linear-gradient(150deg, #03091a 0%, #050d20 45%, #071428 100%)',
    headlineColor: '#f0f8ff',
    subColor: 'rgba(180,215,255,0.52)',
    accentPrimary: '#22d3ee',
    accentSecondary: '#a78bfa',
    ringTrack: 'rgba(34,211,238,0.14)',
    ringStroke: '#22d3ee',
    ringCenter: '#f0f8ff',
    ringCenterSub: 'rgba(180,215,255,0.5)',
    cardBg: 'rgba(255,255,255,0.03)',
    cardBorder: '1px solid rgba(255,255,255,0.07)',
    progressBg: 'rgba(255,255,255,0.06)',
    badgeBg: 'rgba(34,211,238,0.1)',
    badgeBorder: 'rgba(34,211,238,0.25)',
    badgeColor: '#38bdf8',
    footerColor: 'rgba(255,255,255,0.22)',
    gridColor: 'rgba(99,179,237,0.045)',
    statVal: '#f0f8ff',
    statLabel: 'rgba(180,215,255,0.5)',
    statIcon: '#22d3ee' as string,
    statBg: 'rgba(34,211,238,0.07)',
    statBorder: '1px solid rgba(34,211,238,0.18)',
    chipBg: 'rgba(255,255,255,0.04)',
    chipBorder: '1px solid rgba(255,255,255,0.08)',
    chipColor: 'rgba(200,230,255,0.65)',
    chipIconColor: '#22d3ee' as string,
    blob1: 'radial-gradient(circle, rgba(34,211,238,0.12) 0%, transparent 70%)',
    blob2: 'radial-gradient(circle, rgba(167,139,250,0.1) 0%, transparent 70%)',
    blob3: 'radial-gradient(circle, rgba(13,148,136,0.08) 0%, transparent 70%)',
    liveFooterDot: 'rgba(34,211,238,0.5)',
    liveFooterGlow: 'rgba(34,211,238,0.6)',
    eyebrow: 'rgba(56,189,248,0.75)',
  } : {
    bg: 'linear-gradient(150deg, #f0f7ff 0%, #e8f2fd 50%, #dde9fb 100%)',
    headlineColor: '#0c1a3a',
    subColor: 'rgba(15,30,70,0.55)',
    accentPrimary: '#0284c7',
    accentSecondary: '#7c3aed',
    ringTrack: 'rgba(2,132,199,0.14)',
    ringStroke: '#0284c7',
    ringCenter: '#0c1a3a',
    ringCenterSub: 'rgba(15,30,70,0.5)',
    cardBg: 'rgba(255,255,255,0.85)',
    cardBorder: '1px solid rgba(2,132,199,0.14)',
    progressBg: 'rgba(2,132,199,0.08)',
    badgeBg: 'rgba(2,132,199,0.09)',
    badgeBorder: 'rgba(2,132,199,0.22)',
    badgeColor: '#0284c7',
    footerColor: 'rgba(15,30,70,0.35)',
    gridColor: 'rgba(14,165,233,0.04)',
    statVal: '#0c1a3a',
    statLabel: 'rgba(15,30,70,0.5)',
    statIcon: '#0284c7' as string,
    statBg: 'rgba(2,132,199,0.07)',
    statBorder: '1px solid rgba(2,132,199,0.14)',
    chipBg: 'rgba(2,132,199,0.06)',
    chipBorder: '1px solid rgba(2,132,199,0.14)',
    chipColor: 'rgba(15,30,70,0.7)',
    chipIconColor: '#0284c7' as string,
    blob1: 'radial-gradient(circle, rgba(14,165,233,0.1) 0%, transparent 70%)',
    blob2: 'radial-gradient(circle, rgba(99,102,241,0.08) 0%, transparent 70%)',
    blob3: 'radial-gradient(circle, rgba(13,148,136,0.07) 0%, transparent 70%)',
    liveFooterDot: 'rgba(2,132,199,0.5)',
    liveFooterGlow: 'rgba(2,132,199,0.6)',
    eyebrow: 'rgba(2,132,199,0.85)',
  }


  const [ssoEnabled, setSsoEnabled] = useState(false)
  const [ssoDomain, setSsoDomain] = useState('')
  const [settingsLoaded, setSettingsLoaded] = useState(false)
  const [showPasswordFallback, setShowPasswordFallback] = useState(false)
  const [forgotSent, setForgotSent] = useState(false)
  const [forgotLoading, setForgotLoading] = useState(false)
  const [forgotError, setForgotError] = useState<string | null>(null)

  useInjectAuroraKeyframes()

  const stat1 = useCountUp(14.3, 'K', 1, 900, 1200)
  const stat2 = useCountUp(4.2, 'd', 1, 900, 1200)
  const stat3 = useCountUp(96, '%', 0, 900, 1200)

  useEffect(() => {
    fetchAppSettings()
      .then(s => {
        setSsoEnabled(s.sso_enabled === 'true')
        setSsoDomain(s.sso_domain || '')
      })
      .catch(() => {})
      .finally(() => setSettingsLoaded(true))
  }, [])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    setLoading(true)
    const { error } = await signIn(email, password)
    if (error) setError(error.message)
    setLoading(false)
  }

  const handleSSOSignIn = async () => {
    setError(null)
    if (!ssoDomain) { setError('SSO domain not configured — contact your administrator.'); return }
    setLoading(true)
    const { error } = await signInWithSSO(ssoDomain)
    if (error) setError(error.message)
    setLoading(false)
  }

  const handleForgotPassword = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!email) { setForgotError('Enter your email address above first.'); return }
    setForgotLoading(true)
    setForgotError(null)
    try {
      const res = await fetch('/api/forgot-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        setForgotError(data.error || 'Failed to send reset email. Please try again.')
      } else if (data.rateLimited) {
        const mins = Math.ceil((data.retryAfter ?? 900) / 60)
        setForgotError(`Too many reset requests. Please wait ${mins} minute${mins !== 1 ? 's' : ''} before trying again.`)
      } else {
        setForgotSent(true)
      }
    } catch {
      setForgotError('Network error. Please try again.')
    } finally {
      setForgotLoading(false)
    }
  }

  const showSSO = ssoEnabled && settingsLoaded && !showPasswordFallback
  const showPassword = !ssoEnabled || showPasswordFallback

  /* Right panel theme values */
  const rp = {
    bg: isDark ? '#0d1117' : '#ffffff',
    label: isDark ? 'rgba(255,255,255,0.5)' : '#475569',
    title: isDark ? '#f0f6ff' : '#1e293b',
    sub: isDark ? 'rgba(255,255,255,0.4)' : '#64748b',
    divLine: isDark ? 'rgba(255,255,255,0.08)' : '#e2e8f0',
    divText: isDark ? 'rgba(255,255,255,0.3)' : '#94a3b8',
    forgot: isDark ? '#38bdf8' : '#0284c7',
    ssoBorder: isDark ? 'rgba(14,165,233,0.3)' : 'rgba(14,165,233,0.4)',
    ssoColor: isDark ? '#38bdf8' : '#0284c7',
    footer: isDark ? 'rgba(255,255,255,0.2)' : '#94a3b8',
    logoBg: isDark ? 'rgba(255,255,255,0.92)' : '#f8fafc',
    logoBorder: isDark ? 'rgba(255,255,255,0.08)' : '#e2e8f0',
    glow1: isDark ? 'rgba(14,165,233,0.07)' : 'rgba(14,165,233,0.05)',
    glow2: isDark ? 'rgba(45,212,191,0.06)' : 'rgba(45,212,191,0.04)',
  }

  const btnStyle: React.CSSProperties = {
    width: '100%', padding: '10px', border: 'none', borderRadius: 8,
    background: 'linear-gradient(135deg, #0ea5e9 0%, #0d9488 50%, #0ea5e9 100%)',
    backgroundSize: '200% auto',
    color: '#fff', fontSize: 14, fontWeight: 600, cursor: 'pointer',
    display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8,
    animation: 'alp-fadeUp 0.5s 0.55s ease both, alp-shimmer 3.5s 2s linear infinite',
  }

  return (
    <div
      className="min-h-screen flex items-center justify-center"
      style={{
        background: isDark ? '#020817' : '#f0f4f8',
        fontFamily: 'var(--font-sans, system-ui, sans-serif)',
        transition: 'background 0.4s',
        position: 'relative',
        minHeight: '100vh',
        width: '100vw',
        overflow: 'hidden',
      }}
    >
      {/* Full-screen radial glow overlay */}
      <div
        style={{
          position: 'absolute',
          inset: 0,
          pointerEvents: 'none',
          zIndex: 0,
          background: isDark
            ? 'radial-gradient(ellipse 80% 60% at 50% 0%, rgba(6,182,212,0.08) 0%, transparent 70%)'
            : 'radial-gradient(ellipse 80% 60% at 50% 0%, rgba(6,182,212,0.06) 0%, transparent 70%)',
        }}
      />

      {/* Theme toggle — fixed top-right */}
      <button
        onClick={toggleTheme}
        title={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
        style={{
          position: 'fixed',
          top: 22,
          right: 22,
          zIndex: 100,
          width: 72,
          height: 32,
          borderRadius: 999,
          border: isDark
            ? '1px solid rgba(6,182,212,0.25)'
            : '1px solid rgba(0,0,0,0.12)',
          background: isDark
            ? 'rgba(6,182,212,0.12)'
            : 'rgba(0,0,0,0.06)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: 0,
          cursor: 'pointer',
          transition: 'background 0.3s, border-color 0.3s',
          boxShadow: isDark
            ? '0 2px 12px rgba(6,182,212,0.08)'
            : '0 2px 8px rgba(0,0,0,0.04)',
          overflow: 'hidden',
        }}
      >
        <span
          style={{
            flex: 1,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: isDark ? '#22d3ee' : '#0891b2',
            opacity: isDark ? 0.5 : 1,
            fontSize: 18,
            transition: 'color 0.3s, opacity 0.3s',
          }}
        >
          <LightModeRounded />
        </span>
        <span
          style={{
            width: 28,
            height: 28,
            borderRadius: '50%',
            background: isDark
              ? '#22d3ee'
              : '#0891b2',
            position: 'absolute',
            left: isDark ? 38 : 6,
            top: 2,
            transition: 'left 0.3s, background 0.3s',
            boxShadow: isDark
              ? '0 0 0 2px rgba(6,182,212,0.15)'
              : '0 0 0 2px rgba(8,145,178,0.12)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          {isDark ? (
            <DarkModeRounded sx={{ color: '#fff', fontSize: 18 }} />
          ) : (
            <LightModeRounded sx={{ color: '#fff', fontSize: 18 }} />
          )}
        </span>
        <span
          style={{
            flex: 1,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: isDark ? '#22d3ee' : '#0891b2',
            opacity: isDark ? 1 : 0.5,
            fontSize: 18,
            transition: 'color 0.3s, opacity 0.3s',
          }}
        >
          <DarkModeRounded />
        </span>
      </button>

      {/* Main card */}
      <div
        className="flex flex-col lg:flex-row"
        style={{
          width: '100%',
          maxWidth: 1000,
          borderRadius: 20,
          overflow: 'hidden',
          boxShadow: isDark
            ? '0 25px 80px rgba(0,0,0,0.7), 0 0 0 1px rgba(6,182,212,0.1)'
            : '0 20px 60px rgba(0,0,0,0.1), 0 0 0 1px rgba(6,182,212,0.12)',
          position: 'relative',
          zIndex: 1,
        }}
      >
        {/* MOBILE BANNER */}
        <div
          className="flex lg:hidden flex-col"
          style={{
            background: isDark
              ? 'linear-gradient(160deg, #03091a 0%, #050f22 60%, #071428 100%)'
              : 'linear-gradient(160deg, #f0f7ff 0%, #e8f2fd 60%, #dde9fb 100%)',
            position: 'relative',
            zIndex: 2,
            overflow: 'hidden',
          }}
        >
          <Blob w={200} h={200} color={LP.blob1} top={-60} left={-60} opacity={1} anim="alp-drift1 12s ease-in-out infinite" blur={70} />
          <Blob w={160} h={160} color={LP.blob2} bottom={-40} right={-40} opacity={1} anim="alp-drift2 14s ease-in-out infinite" blur={60} />
          <GridOverlay size={36} color={LP.gridColor} />

          <div style={{ position: 'relative', zIndex: 2, padding: '28px 24px 16px' }}>
            {/* Status badge */}
            <div style={{ display: 'inline-flex', alignItems: 'center', gap: 6, background: LP.badgeBg, border: `1px solid ${LP.badgeBorder}`, borderRadius: 999, padding: '3px 10px', fontSize: 10, color: LP.badgeColor, marginBottom: 12, animation: 'alp-fadeUp 0.5s 0.15s ease both' }}>
              <LiveDot /> All systems operational
            </div>
            {/* Headline */}
            <div style={{ fontSize: 22, fontWeight: 900, lineHeight: 1.18, letterSpacing: '-0.5px', color: LP.headlineColor, marginBottom: 6, animation: 'alp-fadeUp 0.5s 0.25s ease both' }}>
              Ship on{' '}
              <span style={{ background: isDark ? 'linear-gradient(90deg, #22d3ee, #a78bfa)' : 'linear-gradient(90deg, #0284c7, #7c3aed)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent', backgroundClip: 'text' }}>
                schedule.
              </span>
            </div>
            <div style={{ fontSize: 12, color: LP.subColor, lineHeight: 1.65, animation: 'alp-fadeUp 0.5s 0.35s ease both' }}>
              Real-time sprint velocity for teams that deliver.
            </div>
            {/* Sprint progress preview */}
            <div style={{ marginTop: 16, animation: 'alp-fadeUp 0.5s 0.45s ease both' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 5 }}>
                <span style={{ fontSize: 9, color: LP.subColor, letterSpacing: '0.03em' }}>Sprint 24 · API Gateway v3</span>
                <span style={{ fontSize: 9, color: LP.accentPrimary, fontWeight: 600 }}>78%</span>
              </div>
              <div style={{ height: 4, borderRadius: 99, background: LP.progressBg, overflow: 'hidden' }}>
                <div style={{ width: '78%', height: '100%', borderRadius: 99, background: isDark ? 'linear-gradient(90deg, #22d3ee, #4ade80)' : 'linear-gradient(90deg, #0284c7, #16a34a)', transformOrigin: 'left', animation: 'alp-bar-fill 1.2s 0.8s ease both' }} />
              </div>
            </div>
          </div>

          {/* Mobile stats strip */}
          <div style={{ display: 'flex', gap: 8, padding: '14px 24px 18px', borderTop: isDark ? '1px solid rgba(255,255,255,0.05)' : '1px solid rgba(2,132,199,0.1)', background: isDark ? 'rgba(0,0,0,0.12)' : 'rgba(255,255,255,0.7)', zIndex: 2, position: 'relative' }}>
            <StatPill val={stat1} label="Projects" icon={<Inventory2Rounded sx={{ fontSize: 13, color: LP.statIcon }} />} valColor={LP.statVal} labelColor={LP.statLabel} bg={LP.statBg} border={LP.statBorder} />
            <StatPill val={stat2} label="Avg Lead" icon={<TimerRounded sx={{ fontSize: 13, color: LP.statIcon }} />} valColor={LP.statVal} labelColor={LP.statLabel} bg={LP.statBg} border={LP.statBorder} />
            <StatPill val={stat3} label="On-Time" icon={<TaskAltRounded sx={{ fontSize: 13, color: LP.statIcon }} />} valColor={LP.statVal} labelColor={LP.statLabel} bg={LP.statBg} border={LP.statBorder} />
          </div>
        </div>

        {/* DESKTOP LEFT PANEL */}
        <div
          className="hidden lg:flex flex-col"
          style={{
            flex: 1,
            minHeight: 560,
            position: 'relative',
            background: LP.bg,
            padding: '44px 42px 40px',
            transition: 'background 0.4s',
            overflow: 'hidden',
          }}
        >
          {/* BG layers */}
          <GridOverlay size={40} color={LP.gridColor} />
          <Blob w={320} h={320} color={LP.blob1} top={-120} left={-80} opacity={1} anim="alp-drift1 14s ease-in-out infinite" blur={90} />
          <Blob w={260} h={260} color={LP.blob2} bottom={-80} right={-60} opacity={1} anim="alp-drift2 16s ease-in-out infinite" blur={80} />
          <Blob w={180} h={180} color={LP.blob3} top="45%" left="50%" opacity={1} anim="alp-drift3 11s ease-in-out infinite" blur={60} />

          {/* CONTENT — flex column fills panel height */}
          <div style={{ position: 'relative', zIndex: 2, display: 'flex', flexDirection: 'column', height: '100%' }}>

            {/* ── TOP: Headline + Velocity Ring side-by-side ── */}
            <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 14 }}>

              {/* Text block */}
              <div style={{ flex: 1 }}>
                {/* Eyebrow */}
                <div style={{ fontSize: 9, fontWeight: 600, letterSpacing: '0.2em', color: LP.eyebrow, textTransform: 'uppercase', marginBottom: 14, animation: 'alp-fadeUp 0.6s 0.2s ease both' }}>
                  PROJECT DELIVERY
                </div>
                {/* Headline */}
                <div style={{ fontSize: 34, fontWeight: 900, lineHeight: 1.12, letterSpacing: '-1.2px', color: LP.headlineColor, animation: 'alp-fadeUp 0.6s 0.3s ease both' }}>
                  <div>Ship on</div>
                  <div>
                    <span style={{
                      background: isDark
                        ? 'linear-gradient(100deg, #22d3ee 0%, #a78bfa 100%)'
                        : 'linear-gradient(100deg, #0284c7 0%, #7c3aed 100%)',
                      WebkitBackgroundClip: 'text',
                      WebkitTextFillColor: 'transparent',
                      backgroundClip: 'text',
                    }}>
                      schedule.
                    </span>
                  </div>
                </div>
                {/* Sub */}
                <div style={{ fontSize: 12, color: LP.subColor, lineHeight: 1.7, maxWidth: 230, marginTop: 10, animation: 'alp-fadeUp 0.6s 0.42s ease both' }}>
                  Real-time sprint velocity for teams that deliver.
                </div>
                {/* Feature chips */}
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 18, animation: 'alp-fadeUp 0.6s 0.52s ease both' }}>
                  <span style={{ fontSize: 10, fontWeight: 500, padding: '3px 10px', borderRadius: 999, background: LP.chipBg, border: LP.chipBorder, color: LP.chipColor, display: 'flex', alignItems: 'center', gap: 5, transition: 'background 0.35s, color 0.35s' }}>
                    <BoltRounded sx={{ color: LP.chipIconColor, fontSize: 12 }} /> Live sprints
                  </span>
                  <span style={{ fontSize: 10, fontWeight: 500, padding: '3px 10px', borderRadius: 999, background: LP.chipBg, border: LP.chipBorder, color: LP.chipColor, display: 'flex', alignItems: 'center', gap: 5, transition: 'background 0.35s, color 0.35s' }}>
                    <InsightsRounded sx={{ color: LP.chipIconColor, fontSize: 12 }} /> Velocity analytics
                  </span>
                  <span style={{ fontSize: 10, fontWeight: 500, padding: '3px 10px', borderRadius: 999, background: LP.chipBg, border: LP.chipBorder, color: LP.chipColor, display: 'flex', alignItems: 'center', gap: 5, transition: 'background 0.35s, color 0.35s' }}>
                    <LockRounded sx={{ color: LP.chipIconColor, fontSize: 12 }} /> Enterprise SSO
                  </span>
                </div>
              </div>

              {/* Velocity Ring SVG */}
              <div style={{ flexShrink: 0, textAlign: 'center', animation: 'alp-fadeIn 0.8s 0.55s ease both', marginTop: 4 }}>
                <svg width="108" height="108" viewBox="0 0 108 108" style={{ overflow: 'visible' }}>
                  <defs>
                    <linearGradient id="ringGradDark" x1="0%" y1="0%" x2="100%" y2="100%">
                      <stop offset="0%" stopColor="#22d3ee" />
                      <stop offset="100%" stopColor="#a78bfa" />
                    </linearGradient>
                  </defs>
                  {/* Outer faint ring */}
                  <circle cx="54" cy="54" r="50" fill="none" stroke={LP.ringTrack} strokeWidth="1" opacity="0.5" />
                  {/* Track ring */}
                  <circle cx="54" cy="54" r="43" fill="none" stroke={LP.ringTrack} strokeWidth="8" strokeLinecap="round" />
                  {/* Progress arc — 96% of 2π×43 ≈ 270.2 → 259.4 */}
                  <circle
                    cx="54" cy="54" r="43"
                    fill="none"
                    stroke={isDark ? 'url(#ringGradDark)' : LP.ringStroke}
                    strokeWidth="8"
                    strokeLinecap="round"
                    style={{
                      strokeDasharray: '259.4 270.2',
                      transformOrigin: '54px 54px',
                      transform: 'rotate(-90deg)',
                      animation: 'alp-ring-fill 2s 0.7s ease both',
                      filter: isDark ? 'drop-shadow(0 0 8px rgba(34,211,238,0.55))' : 'drop-shadow(0 0 4px rgba(2,132,199,0.35))',
                    }}
                  />
                  {/* Center value */}
                  <text x="54" y="49" textAnchor="middle" dominantBaseline="middle" fill={LP.ringCenter} fontSize="19" fontWeight="800" fontFamily="system-ui, sans-serif">96%</text>
                  <text x="54" y="64" textAnchor="middle" dominantBaseline="middle" fill={LP.ringCenterSub} fontSize="8" fontFamily="system-ui, sans-serif" letterSpacing="1.5">ON-TIME</text>
                </svg>
                <div style={{ fontSize: 8, color: LP.subColor, letterSpacing: '0.12em', textTransform: 'uppercase', marginTop: 4, transition: 'color 0.35s' }}>Velocity</div>
              </div>
            </div>

            {/* ── SPRINT CARDS ── */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 9, marginTop: 26 }}>

              {/* Card 1: Live */}
              <div style={{ background: LP.cardBg, border: LP.cardBorder, borderRadius: 11, padding: '11px 14px', backdropFilter: 'blur(12px)', transition: 'background 0.35s, border-color 0.35s', animation: 'alp-card-in 0.5s 0.65s ease both' }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                  <div>
                    <div style={{ fontSize: 12, fontWeight: 700, color: LP.headlineColor, lineHeight: 1, transition: 'color 0.35s' }}>API Gateway v3</div>
                    <div style={{ fontSize: 9, color: LP.subColor, marginTop: 2, transition: 'color 0.35s' }}>Sprint 24 · Backend</div>
                  </div>
                  <span style={{ fontSize: 9, fontWeight: 600, padding: '2px 8px', borderRadius: 999, background: 'rgba(74,222,128,0.12)', color: '#4ade80', border: '1px solid rgba(74,222,128,0.22)', whiteSpace: 'nowrap', flexShrink: 0 }}>● LIVE</span>
                </div>
                <div style={{ height: 4, borderRadius: 99, background: LP.progressBg, overflow: 'hidden' }}>
                  <div style={{ width: '78%', height: '100%', borderRadius: 99, background: isDark ? 'linear-gradient(90deg, #22d3ee, #4ade80)' : 'linear-gradient(90deg, #0284c7, #16a34a)', transformOrigin: 'left', animation: 'alp-bar-fill 1.2s 1.0s ease both' }} />
                </div>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 5 }}>
                  <span style={{ fontSize: 9, color: LP.subColor, transition: 'color 0.35s' }}>78% complete</span>
                  <span style={{ fontSize: 9, color: LP.subColor, transition: 'color 0.35s' }}>3 days left</span>
                </div>
              </div>

              {/* Card 2: Review */}
              <div style={{ background: LP.cardBg, border: LP.cardBorder, borderRadius: 11, padding: '11px 14px', backdropFilter: 'blur(12px)', transition: 'background 0.35s, border-color 0.35s', animation: 'alp-card-in 0.5s 0.8s ease both' }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                  <div>
                    <div style={{ fontSize: 12, fontWeight: 700, color: LP.headlineColor, lineHeight: 1, transition: 'color 0.35s' }}>Dashboard UI</div>
                    <div style={{ fontSize: 9, color: LP.subColor, marginTop: 2, transition: 'color 0.35s' }}>Sprint 23 · Frontend</div>
                  </div>
                  <span style={{ fontSize: 9, fontWeight: 600, padding: '2px 8px', borderRadius: 999, background: 'rgba(167,139,250,0.12)', color: isDark ? '#a78bfa' : '#7c3aed', border: isDark ? '1px solid rgba(167,139,250,0.22)' : '1px solid rgba(124,58,237,0.22)', whiteSpace: 'nowrap', flexShrink: 0 }}>● REVIEW</span>
                </div>
                <div style={{ height: 4, borderRadius: 99, background: LP.progressBg, overflow: 'hidden' }}>
                  <div style={{ width: '92%', height: '100%', borderRadius: 99, background: isDark ? 'linear-gradient(90deg, #a78bfa, #22d3ee)' : 'linear-gradient(90deg, #7c3aed, #0284c7)', transformOrigin: 'left', animation: 'alp-bar-fill 1.2s 1.15s ease both' }} />
                </div>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 5 }}>
                  <span style={{ fontSize: 9, color: LP.subColor, transition: 'color 0.35s' }}>92% complete</span>
                  <span style={{ fontSize: 9, color: LP.subColor, transition: 'color 0.35s' }}>1 day left</span>
                </div>
              </div>

              {/* Card 3: Build */}
              <div style={{ background: LP.cardBg, border: LP.cardBorder, borderRadius: 11, padding: '11px 14px', backdropFilter: 'blur(12px)', transition: 'background 0.35s, border-color 0.35s', animation: 'alp-card-in 0.5s 0.95s ease both' }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                  <div>
                    <div style={{ fontSize: 12, fontWeight: 700, color: LP.headlineColor, lineHeight: 1, transition: 'color 0.35s' }}>Data Pipeline</div>
                    <div style={{ fontSize: 9, color: LP.subColor, marginTop: 2, transition: 'color 0.35s' }}>Sprint 25 · Data Eng</div>
                  </div>
                  <span style={{ fontSize: 9, fontWeight: 600, padding: '2px 8px', borderRadius: 999, background: 'rgba(251,191,36,0.12)', color: '#fbbf24', border: '1px solid rgba(251,191,36,0.22)', whiteSpace: 'nowrap', flexShrink: 0 }}>● BUILD</span>
                </div>
                <div style={{ height: 4, borderRadius: 99, background: LP.progressBg, overflow: 'hidden' }}>
                  <div style={{ width: '61%', height: '100%', borderRadius: 99, background: isDark ? 'linear-gradient(90deg, #fbbf24, #f97316)' : 'linear-gradient(90deg, #d97706, #ea580c)', transformOrigin: 'left', animation: 'alp-bar-fill 1.2s 1.3s ease both' }} />
                </div>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 5 }}>
                  <span style={{ fontSize: 9, color: LP.subColor, transition: 'color 0.35s' }}>61% complete</span>
                  <span style={{ fontSize: 9, color: LP.subColor, transition: 'color 0.35s' }}>6 days left</span>
                </div>
              </div>

            </div>

            {/* ── BOTTOM: Stats + Footer ── */}
            <div style={{ marginTop: 'auto', paddingTop: 22 }}>
              {/* Stat pills */}
              <div style={{ display: 'flex', gap: 8, marginBottom: 18, animation: 'alp-fadeUp 0.6s 1.3s ease both' }}>
                <StatPill val={stat1} label="Projects" icon={<Inventory2Rounded sx={{ fontSize: 13, color: LP.statIcon }} />} valColor={LP.statVal} labelColor={LP.statLabel} bg={LP.statBg} border={LP.statBorder} />
                <StatPill val={stat2} label="Avg Lead" icon={<TimerRounded sx={{ fontSize: 13, color: LP.statIcon }} />} valColor={LP.statVal} labelColor={LP.statLabel} bg={LP.statBg} border={LP.statBorder} />
                <StatPill val={stat3} label="On-Time" icon={<TaskAltRounded sx={{ fontSize: 13, color: LP.statIcon }} />} valColor={LP.statVal} labelColor={LP.statLabel} bg={LP.statBg} border={LP.statBorder} />
              </div>
              {/* Footer */}
              <div style={{ fontSize: 10, color: LP.footerColor, display: 'flex', alignItems: 'center', gap: 8, transition: 'color 0.35s' }}>
                <span style={{ display: 'inline-block', width: 6, height: 6, borderRadius: '50%', background: LP.liveFooterDot, boxShadow: `0 0 6px ${LP.liveFooterGlow}`, flexShrink: 0 }} />
                Secure · Enterprise-ready · Magnit Internal Use Only
              </div>
            </div>

          </div>
        </div>

        {/* RIGHT PANEL (login form) */}
        <div
          className="flex flex-col justify-center w-full lg:w-auto"
          style={{
            flex: '0 0 400px',
            background: isDark ? '#0d1117' : '#ffffff',
            position: 'relative',
            padding: '44px 44px 52px',
            transition: 'background 0.4s',
            minWidth: 0,
          }}
        >
          {/* Ambient glows */}
          <div
            style={{
              position: 'absolute',
              top: -80,
              right: -80,
              width: 220,
              height: 220,
              background: `radial-gradient(circle, ${rp.glow1} 0%, transparent 70%)`,
              pointerEvents: 'none',
              animation: 'alp-drift1 10s ease-in-out infinite',
              zIndex: 1,
            }}
          />
          <div
            style={{
              position: 'absolute',
              bottom: -60,
              left: -60,
              width: 180,
              height: 180,
              background: `radial-gradient(circle, ${rp.glow2} 0%, transparent 70%)`,
              pointerEvents: 'none',
              animation: 'alp-drift2 12s ease-in-out infinite',
              zIndex: 1,
            }}
          />
          <div style={{ position: 'relative', zIndex: 2 }}>
            {/* Logo */}
            {logoUrl && (
              <div className="hidden lg:flex" style={{ justifyContent: 'flex-end', marginBottom: 24, animation: 'alp-fadeUp 0.5s 0.1s ease both' }}>
                <div style={{ background: rp.logoBg, border: `1px solid ${rp.logoBorder}`, borderRadius: 10, padding: '8px 14px', display: 'flex', alignItems: 'center', transition: 'background 0.35s, border-color 0.35s' }}>
                  <img src={logoUrl} alt="Company Logo" style={{ height: 42, width: 'auto', display: 'block', maxWidth: 220 }} />
                </div>
              </div>
            )}
            {logoUrl && (
              <div className="flex lg:hidden" style={{ position: 'absolute', top: -28, right: 0 }}>
                <img src={logoUrl} alt="Company Logo" style={{ height: 26, width: 'auto', display: 'block', maxWidth: 130 }} />
              </div>
            )}

            {/* Heading block */}
            <div
              style={{
                marginBottom: logoUrl ? 20 : 32,
                animation: 'alp-fadeUp 0.5s 0.2s ease both',
              }}
            >
              <div
                style={{
                  fontSize: 10,
                  fontWeight: 600,
                  letterSpacing: '0.14em',
                  color: rp.footer,
                  textTransform: 'uppercase',
                  marginBottom: 10,
                  transition: 'color 0.35s',
                }}
              >
                DELIVERY TRACKER
              </div>
              <h1
                style={{
                  fontSize: 26,
                  fontWeight: 800,
                  color: rp.title,
                  marginBottom: 6,
                  letterSpacing: '-0.5px',
                  transition: 'color 0.35s',
                }}
              >
                Welcome back
              </h1>
              <p
                style={{
                  fontSize: 14,
                  color: rp.sub,
                  marginBottom: 28,
                  transition: 'color 0.35s',
                }}
              >
                Sign in to your account to continue
              </p>
            </div>

            {/* SSO MODE */}
            {showSSO && (
              <div className="space-y-4">
                <button type="button" style={btnStyle} disabled={loading} onClick={handleSSOSignIn}>
                  {loading ? <AutorenewRounded sx={{ fontSize: 17 }} className="animate-spin" /> : <VpnKeyRounded sx={{ fontSize: 17 }} />}
                  {loading ? 'Redirecting to Okta…' : 'Sign in with Okta SSO'}
                </button>
                {error && (
                  <div className="alert alert-error py-2">
                    <ErrorOutlineRounded sx={{ fontSize: 18 }} /><span className="text-sm">{error}</span>
                  </div>
                )}
                <div className="text-center">
                  <button
                    type="button"
                    style={{
                      fontSize: 12,
                      color: rp.footer,
                      background: 'none',
                      border: 'none',
                      cursor: 'pointer',
                      textDecoration: 'underline',
                      textUnderlineOffset: 2,
                      transition: 'color 0.35s',
                    }}
                    onClick={() => {
                      setShowPasswordFallback(true);
                      setError(null);
                    }}
                  >
                    Sign in with email &amp; password instead
                  </button>
                </div>
              </div>
            )}

            {/* PASSWORD MODE */}
            {showPassword && (
              <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: 0 }}>
                {/* EMAIL FIELD */}
                <div style={{ marginBottom: 18, animation: 'alp-fadeUp 0.5s 0.35s ease both' }}>
                  <label
                    style={{
                      display: 'block',
                      fontSize: 12,
                      fontWeight: 500,
                      color: rp.label,
                      marginBottom: 6,
                      letterSpacing: '0.02em',
                      transition: 'color 0.35s',
                    }}
                  >
                    Work email
                  </label>
                  <input
                    type="email"
                    className="input input-bordered w-full"
                    style={{ height: 42, fontSize: 14 }}
                    placeholder="you@magnitglobal.com"
                    value={email}
                    onChange={e => setEmail(e.target.value)}
                    required
                    autoFocus
                  />
                </div>
                {/* PASSWORD FIELD */}
                <div style={{ marginBottom: 8, animation: 'alp-fadeUp 0.5s 0.45s ease both' }}>
                  <label
                    style={{
                      display: 'block',
                      fontSize: 12,
                      fontWeight: 500,
                      color: rp.label,
                      marginBottom: 6,
                      letterSpacing: '0.02em',
                      transition: 'color 0.35s',
                    }}
                  >
                    Password
                  </label>
                  <input
                    type="password"
                    className="input input-bordered w-full"
                    style={{ height: 42, fontSize: 14 }}
                    placeholder="••••••••"
                    value={password}
                    onChange={e => setPassword(e.target.value)}
                    required
                  />
                </div>
                {/* ERROR */}
                {error && (
                  <div className="alert alert-error py-2 mt-2">
                    <ErrorOutlineRounded sx={{ fontSize: 18 }} /><span className="text-sm">{error}</span>
                  </div>
                )}
                {/* SUBMIT BUTTON */}
                <button type="submit" style={{ ...btnStyle, marginTop: 18 }} disabled={loading}>
                  {loading ? <AutorenewRounded sx={{ fontSize: 17 }} className="animate-spin" /> : null}
                  {loading ? 'Signing in...' : 'Sign in'}
                  {!loading && <ArrowForwardRounded sx={{ fontSize: 17 }} />}
                </button>
                {/* FORGOT PASSWORD */}
                {!forgotSent ? (
                  <div style={{ textAlign: 'center', marginTop: 12, animation: 'alp-fadeUp 0.5s 0.65s ease both' }}>
                    <button
                      type="button"
                      onClick={handleForgotPassword}
                      disabled={forgotLoading}
                      style={{
                        fontSize: 12,
                        color: rp.forgot,
                        background: 'none',
                        border: 'none',
                        cursor: 'pointer',
                        textDecoration: 'underline',
                        textUnderlineOffset: 2,
                        transition: 'color 0.35s',
                      }}
                    >
                      {forgotLoading ? 'Sending…' : 'Forgot password?'}
                    </button>
                    {forgotError && <p className="text-xs text-error mt-1">{forgotError}</p>}
                  </div>
                ) : (
                  <div className="alert alert-success py-2 mt-3">
                    <span className="text-sm">✓ Password reset email sent — check your inbox.</span>
                  </div>
                )}
                {/* SSO DIVIDER + BUTTON (when !ssoEnabled) */}
                {!ssoEnabled && (
                  <>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 10, margin: '20px 0', animation: 'alp-fadeUp 0.5s 0.7s ease both' }}>
                      <div style={{ flex: 1, height: 1, background: rp.divLine, transition: 'background 0.35s' }} />
                      <span style={{ fontSize: 11, color: rp.divText, transition: 'color 0.35s' }}>or continue with</span>
                      <div style={{ flex: 1, height: 1, background: rp.divLine, transition: 'background 0.35s' }} />
                    </div>
                    <button
                      type="button"
                      onClick={handleSSOSignIn}
                      style={{
                        width: '100%',
                        padding: '9px',
                        background: 'transparent',
                        border: `1px solid ${rp.ssoBorder}`,
                        borderRadius: 8,
                        color: rp.ssoColor,
                        fontSize: 13,
                        fontWeight: 500,
                        cursor: 'pointer',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        gap: 8,
                        animation: 'alp-fadeUp 0.5s 0.75s ease both',
                        transition: 'border-color 0.2s, color 0.35s',
                      }}
                    >
                      <VpnKeyRounded sx={{ fontSize: 17 }} />
                      Sign in with Okta SSO
                    </button>
                  </>
                )}
                {/* BACK TO SSO (when ssoEnabled && showPasswordFallback) */}
                {ssoEnabled && showPasswordFallback && (
                  <div style={{ textAlign: 'center', marginTop: 16 }}>
                    <button
                      type="button"
                      onClick={() => {
                        setShowPasswordFallback(false);
                        setError(null);
                      }}
                      style={{
                        fontSize: 12,
                        color: rp.footer,
                        background: 'none',
                        border: 'none',
                        cursor: 'pointer',
                        textDecoration: 'underline',
                        textUnderlineOffset: 2,
                        transition: 'color 0.35s',
                      }}
                    >
                      <ArrowBackRounded sx={{ fontSize: 14 }} /> Back to Okta SSO login
                    </button>
                  </div>
                )}
              </form>
            )}
            {/* FOOTER */}
            <p
              style={{
                fontSize: 12,
                color: rp.footer,
                textAlign: 'center',
                marginTop: 24,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 6,
                transition: 'color 0.35s',
                animation: 'alp-fadeIn 0.6s 1s ease both',
              }}
            >
              <LockRounded sx={{ fontSize: 14, opacity: 0.7 }} /> Protected by enterprise-grade security
            </p>
          </div>
        </div>
      </div>
      {/* Responsive padding for mobile right panel */}
      <style>
        {`
          @media (max-width: 640px) {
            .flex.lg\\:flex-row > .flex-col.justify-center.w-full.lg\\:w-auto {
              padding: 32px 24px 40px !important;
            }
          }
        `}
      </style>
    </div>
  )
}

/* ── DaaS Keyframes ── */
if (typeof window !== 'undefined' && typeof document !== 'undefined') {
  if (!document.getElementById('alp-daas-kf')) {
    const style = document.createElement('style')
    style.id = 'alp-daas-kf'
    style.textContent = `
@keyframes alp-ring-fill {
  from { stroke-dasharray: 0 270.2; }
  to   { stroke-dasharray: 259.4 270.2; }
}
@keyframes alp-bar-fill {
  from { transform: scaleX(0); }
  to   { transform: scaleX(1); }
}
@keyframes alp-card-in {
  from { opacity: 0; transform: translateX(-14px); }
  to   { opacity: 1; transform: translateX(0); }
}
@keyframes alp-node-pulse {
  0%,100% { transform: scale(1); opacity: 0.6; }
  50% { transform: scale(1.15); opacity: 1; }
}
@keyframes alp-flow-dash {
  0% { stroke-dashoffset: 200; }
  100% { stroke-dashoffset: 0; }
}
@keyframes alp-data-float {
  0%,100% { transform: translateY(0); }
  50% { transform: translateY(-6px); }
}
@keyframes alp-scan-line {
  0% { transform: translateY(-100%); opacity: 0; }
  10% { opacity: 0.4; }
  90% { opacity: 0.4; }
  100% { transform: translateY(100%); opacity: 0; }
}
    `
    document.head.appendChild(style)
  }
}
