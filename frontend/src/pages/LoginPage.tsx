/**
 * LoginPage — "The Terminal" design
 * Hacker/IDE aesthetic: code rain background, monospace type,
 * terminal-style login prompt, glowing green accents.
 *
 * Handles two states:
 *   1. setup_required = true  → first-run "Create Admin Account"
 *   2. setup_required = false → standard login
 */
import { useState, useEffect, useRef, FormEvent } from 'react';
import { useAuthStore } from '../stores/authStore';
import { apiClient } from '../api/client';

/* ─── Code Rain Canvas ─── */
const CODE_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789{}[]()<>=/\\|;:.,+-*&^%$#@!~`';

function CodeRain() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let animId: number;
    const resize = () => {
      canvas.width = window.innerWidth * 2;
      canvas.height = window.innerHeight * 2;
      ctx.setTransform(2, 0, 0, 2, 0, 0);
    };
    resize();

    const cols = Math.floor(window.innerWidth / 18);
    const drops: number[] = Array.from({ length: cols }, () => Math.random() * -100);

    const draw = () => {
      ctx.fillStyle = 'rgba(0, 0, 0, 0.06)';
      ctx.fillRect(0, 0, window.innerWidth, window.innerHeight);
      ctx.font = '14px monospace';

      drops.forEach((y, i) => {
        const char = CODE_CHARS[Math.floor(Math.random() * CODE_CHARS.length)];
        const brightness = Math.random();
        ctx.fillStyle = brightness > 0.7
          ? '#4ade80'
          : `rgba(74, 222, 128, ${0.15 + brightness * 0.3})`;
        ctx.fillText(char, i * 18, y);
        drops[i] = y > window.innerHeight + Math.random() * 500 ? 0 : y + 16;
      });
      animId = requestAnimationFrame(draw);
    };
    animId = requestAnimationFrame(draw);

    window.addEventListener('resize', resize);
    return () => {
      cancelAnimationFrame(animId);
      window.removeEventListener('resize', resize);
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      style={{
        position: 'fixed', inset: 0, width: '100%', height: '100%',
        background: '#0a0a0a', zIndex: 0,
      }}
    />
  );
}

/* ─── Typing Animation ─── */
function TypingText({ text, delay = 0 }: { text: string; delay?: number }) {
  const [shown, setShown] = useState('');
  useEffect(() => {
    let i = 0;
    const timeout = setTimeout(() => {
      const interval = setInterval(() => {
        if (i < text.length) { setShown(text.slice(0, i + 1)); i++; }
        else clearInterval(interval);
      }, 45);
      return () => clearInterval(interval);
    }, delay);
    return () => clearTimeout(timeout);
  }, [text, delay]);
  return <span>{shown}<span style={{ animation: 'pulse 1s infinite' }}>▊</span></span>;
}

/* ─── Icons (inline SVGs — no extra dependency) ─── */
const TermIcon = () => (
  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#4ade80" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>
  </svg>
);
const LockIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>
  </svg>
);
const ArrowIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/>
  </svg>
);
const EyeIcon = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>
  </svg>
);
const EyeOffIcon = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/>
    <line x1="1" y1="1" x2="23" y2="23"/>
  </svg>
);
const ShieldIcon = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#4ade80" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
  </svg>
);
const CpuIcon = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/><line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/><line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/>
  </svg>
);
const GitIcon = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>
  </svg>
);
const MailIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/>
  </svg>
);

/* ─── Main Component ─── */
export function LoginPage() {
  const { login } = useAuthStore();
  const [setupRequired, setSetupRequired] = useState<boolean | null>(null);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [email, setEmail] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [showPass, setShowPass] = useState(false);
  const [focused, setFocused] = useState<string | null>(null);

  useEffect(() => {
    apiClient.get('/api/auth/setup-required')
      .then((r) => setSetupRequired(r.data.setup_required))
      .catch(() => setSetupRequired(false));
  }, []);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      const endpoint = setupRequired ? '/api/auth/setup' : '/api/auth/login';
      const body: Record<string, string> = { username, password };
      if (setupRequired) body.email = email;
      const res = await apiClient.post(endpoint, body);
      login(res.data.access_token, res.data.user);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Authentication failed. Try again.');
    } finally {
      setLoading(false);
    }
  }

  /* ─── Loading state ─── */
  if (setupRequired === null) {
    return (
      <div style={{ minHeight: '100vh', background: '#0a0a0a', display: 'grid', placeItems: 'center' }}>
        <div style={{
          width: 32, height: 32,
          border: '2px solid #4ade80', borderTopColor: 'transparent',
          borderRadius: '50%', animation: 'spin 1s linear infinite',
        }} />
        <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
      </div>
    );
  }

  const isSetup = setupRequired;
  const title = isSetup ? 'Initialize System' : 'SurgicalAI';
  const subtitle = isSetup
    ? 'Create your admin account to begin.'
    : 'Precision code edits. Zero collateral.';
  const btnText = isSetup ? 'Create Admin & Launch' : 'Authenticate';

  const inputStyle = (field: string): React.CSSProperties => ({
    width: '100%',
    padding: '12px 16px',
    paddingRight: field === 'pass' ? 48 : 16,
    borderRadius: 8,
    fontFamily: 'monospace',
    fontSize: 14,
    color: '#e2e8f0',
    background: 'rgba(30, 30, 30, 0.8)',
    border: focused === field
      ? '1px solid rgba(74, 222, 128, 0.5)'
      : '1px solid rgba(74, 222, 128, 0.12)',
    boxShadow: focused === field ? '0 0 20px rgba(74, 222, 128, 0.1)' : 'none',
    outline: 'none',
    transition: 'all 0.2s',
  });

  const labelStyle: React.CSSProperties = {
    display: 'flex', alignItems: 'center', gap: 6,
    fontSize: 12, fontFamily: 'monospace',
    color: 'rgba(148, 163, 184, 0.7)',
    marginBottom: 6,
  };

  return (
    <>
      <CodeRain />

      {/* Center glow */}
      <div style={{
        position: 'fixed', inset: 0, zIndex: 1, pointerEvents: 'none',
        background: 'radial-gradient(ellipse at center, rgba(74, 222, 128, 0.08) 0%, transparent 70%)',
      }} />

      <div style={{
        position: 'fixed', inset: 0, zIndex: 2,
        display: 'grid', placeItems: 'center',
        padding: 16,
      }}>
        {/* Terminal card */}
        <div style={{ width: '100%', maxWidth: 440 }}>

          {/* ── Title bar ── */}
          <div style={{
            display: 'flex', alignItems: 'center', gap: 8,
            padding: '10px 16px',
            background: 'rgba(30, 30, 30, 0.95)',
            borderTopLeftRadius: 12, borderTopRightRadius: 12,
            borderBottom: '1px solid rgba(74, 222, 128, 0.15)',
          }}>
            <div style={{ display: 'flex', gap: 6 }}>
              <div style={{ width: 12, height: 12, borderRadius: '50%', background: '#ff5f57' }} />
              <div style={{ width: 12, height: 12, borderRadius: '50%', background: '#febc2e' }} />
              <div style={{ width: 12, height: 12, borderRadius: '50%', background: '#28c840' }} />
            </div>
            <span style={{
              marginLeft: 12, fontSize: 12, fontFamily: 'monospace',
              color: 'rgba(74, 222, 128, 0.6)',
            }}>
              {isSetup ? 'surgicalai@local ~ init' : 'surgicalai@local ~ login'}
            </span>
          </div>

          {/* ── Body ── */}
          <form onSubmit={handleSubmit} style={{
            padding: '32px',
            background: 'rgba(10, 10, 10, 0.92)',
            border: '1px solid rgba(74, 222, 128, 0.1)',
            borderTop: 'none',
            borderBottomLeftRadius: 12, borderBottomRightRadius: 12,
            backdropFilter: 'blur(16px)',
            boxShadow: '0 0 80px rgba(74, 222, 128, 0.08), 0 25px 50px rgba(0,0,0,0.5)',
          }}>

            {/* Logo */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 }}>
              <div style={{
                padding: 8, borderRadius: 8,
                background: 'rgba(74, 222, 128, 0.1)',
                border: '1px solid rgba(74, 222, 128, 0.2)',
                display: 'flex',
              }}>
                <TermIcon />
              </div>
              <div>
                <h1 style={{
                  fontSize: 22, fontWeight: 700, fontFamily: 'monospace',
                  color: '#e2e8f0', margin: 0,
                }}>{title}</h1>
                <div style={{ fontSize: 12, fontFamily: 'monospace', color: '#4ade80' }}>v3.4.0</div>
              </div>
            </div>

            {/* Typing tagline */}
            <div style={{
              fontSize: 14, fontFamily: 'monospace', height: 24,
              color: 'rgba(74, 222, 128, 0.7)', marginBottom: 28,
            }}>
              <TypingText text={subtitle} delay={400} />
            </div>

            {/* Error message */}
            {error && (
              <div style={{
                padding: '10px 14px', borderRadius: 8, marginBottom: 16,
                background: 'rgba(239, 68, 68, 0.1)',
                border: '1px solid rgba(239, 68, 68, 0.3)',
                color: '#f87171', fontSize: 13, fontFamily: 'monospace',
              }}>
                ⚠ {error}
              </div>
            )}

            {/* Fields */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>

              {/* Username */}
              <div>
                <label style={labelStyle}>
                  <span style={{ color: '#4ade80' }}>$</span> username
                </label>
                <input
                  type="text"
                  value={username}
                  onChange={e => setUsername(e.target.value)}
                  onFocus={() => setFocused('user')}
                  onBlur={() => setFocused(null)}
                  placeholder="enter username"
                  autoComplete="username"
                  style={inputStyle('user')}
                />
              </div>

              {/* Email (setup only) */}
              {isSetup && (
                <div>
                  <label style={labelStyle}>
                    <MailIcon /> email
                  </label>
                  <input
                    type="email"
                    value={email}
                    onChange={e => setEmail(e.target.value)}
                    onFocus={() => setFocused('email')}
                    onBlur={() => setFocused(null)}
                    placeholder="admin@example.com"
                    autoComplete="email"
                    style={inputStyle('email')}
                  />
                </div>
              )}

              {/* Password */}
              <div>
                <label style={labelStyle}>
                  <span style={{ color: '#4ade80' }}>$</span> password
                </label>
                <div style={{ position: 'relative' }}>
                  <input
                    type={showPass ? 'text' : 'password'}
                    value={password}
                    onChange={e => setPassword(e.target.value)}
                    onFocus={() => setFocused('pass')}
                    onBlur={() => setFocused(null)}
                    placeholder="••••••••"
                    autoComplete={isSetup ? 'new-password' : 'current-password'}
                    style={inputStyle('pass')}
                  />
                  <button
                    type="button"
                    onClick={() => setShowPass(!showPass)}
                    style={{
                      position: 'absolute', right: 12, top: '50%', transform: 'translateY(-50%)',
                      background: 'none', border: 'none', cursor: 'pointer',
                      color: '#94a3b8', opacity: 0.5,
                      transition: 'opacity 0.2s',
                    }}
                    onMouseEnter={e => (e.currentTarget.style.opacity = '1')}
                    onMouseLeave={e => (e.currentTarget.style.opacity = '0.5')}
                  >
                    {showPass ? <EyeOffIcon /> : <EyeIcon />}
                  </button>
                </div>
              </div>

              {/* Submit */}
              <button
                type="submit"
                disabled={loading || !username || !password}
                style={{
                  width: '100%', padding: '12px 0', borderRadius: 8,
                  fontFamily: 'monospace', fontSize: 14, fontWeight: 600,
                  display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8,
                  background: loading || !username || !password
                    ? 'rgba(74, 222, 128, 0.3)'
                    : 'linear-gradient(135deg, #16a34a, #4ade80)',
                  color: '#0a0a0a',
                  border: 'none', cursor: loading ? 'wait' : 'pointer',
                  boxShadow: '0 0 30px rgba(74, 222, 128, 0.2)',
                  transition: 'all 0.2s',
                  opacity: loading || !username || !password ? 0.6 : 1,
                }}
              >
                {loading ? (
                  <>
                    <div style={{
                      width: 16, height: 16,
                      border: '2px solid #0a0a0a', borderTopColor: 'transparent',
                      borderRadius: '50%', animation: 'spin 1s linear infinite',
                    }} />
                    {isSetup ? 'Initializing...' : 'Authenticating...'}
                  </>
                ) : (
                  <>
                    <LockIcon />
                    {btnText}
                    <ArrowIcon />
                  </>
                )}
              </button>
            </div>

            {/* Status bar */}
            <div style={{
              marginTop: 24, paddingTop: 16,
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              borderTop: '1px solid rgba(74, 222, 128, 0.08)',
              fontSize: 11, fontFamily: 'monospace',
              color: 'rgba(148, 163, 184, 0.4)',
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
                <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                  <ShieldIcon /> AES-256
                </span>
                <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                  <CpuIcon /> Multi-model
                </span>
              </div>
              <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <GitIcon /> main
              </span>
            </div>
          </form>
        </div>
      </div>

      <style>{`
        @keyframes spin { to { transform: rotate(360deg); } }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }
        input::placeholder { color: rgba(148, 163, 184, 0.3) !important; }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { overflow: hidden; }
      `}</style>
    </>
  );
}
