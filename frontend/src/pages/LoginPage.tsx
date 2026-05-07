/**
 * LoginPage — handles two states:
 *   1. setup_required = true  → first-run "Create Admin Account" form
 *   2. setup_required = false → standard login form
 */
import { useState, useEffect, FormEvent } from 'react';
import { useAuthStore } from '../stores/authStore';
import { apiClient } from '../api/client';

const DESIGN: 'A' | 'B' = 'B'; // switch to 'B' to show split-screen.

export function LoginPage() {
  const { login } = useAuthStore();
  const [setupRequired, setSetupRequired] = useState<boolean | null>(null);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [email, setEmail] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

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
      setError(err.response?.data?.detail || 'Login failed. Please try again.');
    } finally {
      setLoading(false);
    }
  }

  if (setupRequired === null) {
    return (
      <div className="sai-fullscreen-root sai-bg-darkmesh flex items-center justify-center">
        <div className="sai-loader" />
        <style>{`
          .sai-fullscreen-root { min-height: 100vh; }
          .sai-bg-darkmesh { background: #0b0f14; position: relative; }
          .sai-loader {
            width: 2rem; height: 2rem;
            border: 2px solid #6366f1;
            border-top-color: transparent;
            border-radius: 9999px;
            animation: sai-spin 1s linear infinite;
          }
          @keyframes sai-spin { to { transform: rotate(360deg); } }
        `}</style>
      </div>
    );
  }

  if (DESIGN === 'A') {
    return (
      <div className="sai-fullscreen-root sai-bg-darkmesh" style={{ display: 'grid', minHeight: '100vh', placeItems: 'center', position: 'relative', overflow: 'hidden' }}>
        <div className="sai-mesh-bg" aria-hidden="true" />
        <div className="sai-card-border" style={{ background: 'linear-gradient(135deg, rgba(139,92,246,0.6), rgba(6,182,212,0.6))', padding: 1, borderRadius: 18, maxWidth: 420, width: '100%', zIndex: 1 }}>
          <div className="sai-glass-card" style={{
            borderRadius: 16,
            background: 'rgba(255,255,255,0.06)',
            border: '1px solid rgba(255,255,255,0.12)',
            boxShadow: '0 10px 30px rgba(0,0,0,0.5)',
            backdropFilter: 'blur(16px) saturate(140%)',
            WebkitBackdropFilter: 'blur(16px) saturate(140%)',
            padding: '2.5rem 2rem 2rem 2rem',
            position: 'relative',
            minWidth: 0,
          }}>
            <div className="sai-header" style={{ marginBottom: 32, textAlign: 'center' }}>
              <span
                className="sai-logo"
                style={{
                  fontWeight: 800,
                  fontSize: 28,
                  letterSpacing: '-0.04em',
                  background: 'linear-gradient(90deg,#8b5cf6,#06b6d4 90%)',
                  WebkitBackgroundClip: 'text',
                  WebkitTextFillColor: 'transparent',
                  backgroundClip: 'text',
                  display: 'inline-block',
                  marginBottom: 4,
                }}
              >
                SurgicalAI
              </span>
              <h1 style={{
                color: '#fff',
                fontWeight: 700,
                fontSize: 22,
                margin: '12px 0 0 0',
                letterSpacing: '-0.01em'
              }}>
                {setupRequired ? 'Create the first admin' : 'Welcome back'}
              </h1>
              <div style={{ color: '#b6b9c9', fontSize: 14, marginTop: 4 }}>
                {setupRequired
                  ? 'Set up your admin credentials to begin'
                  : 'Sign in to your SurgicalAI workspace'}
              </div>
            </div>
            {setupRequired && (
              <div className="sai-firstrun-banner" style={{
                marginBottom: 24,
                padding: '12px 14px',
                borderRadius: 10,
                background: 'rgba(139,92,246,0.10)',
                border: '1px solid rgba(139,92,246,0.22)',
                textAlign: 'left'
              }}>
                <div style={{ color: '#a5b4fc', fontWeight: 600, fontSize: 13 }}>First time setup</div>
                <div style={{ color: '#a5b4fc', opacity: 0.7, fontSize: 12, marginTop: 2 }}>
                  This account will have admin access to manage users.
                </div>
              </div>
            )}
            <form onSubmit={handleSubmit} className="sai-form" style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
              <div>
                <label htmlFor="sai-username" style={{ color: '#e5e7eb', fontSize: 14, fontWeight: 500, marginBottom: 6, display: 'block' }}>Username</label>
                <input
                  id="sai-username"
                  type="text"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="your-username"
                  required
                  minLength={3}
                  className="sai-input"
                  style={{
                    width: '100%',
                    background: 'rgba(255,255,255,0.08)',
                    border: '1px solid rgba(255,255,255,0.13)',
                    borderRadius: 8,
                    padding: '11px 14px',
                    color: '#fff',
                    fontSize: 15,
                    outline: 'none',
                    transition: 'box-shadow 0.15s',
                  }}
                />
              </div>
              {setupRequired && (
                <div>
                  <label htmlFor="sai-email" style={{ color: '#e5e7eb', fontSize: 14, fontWeight: 500, marginBottom: 6, display: 'block' }}>
                    Email <span style={{ color: '#a1a1aa', fontWeight: 400 }}>(optional)</span>
                  </label>
                  <input
                    id="sai-email"
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="you@example.com"
                    className="sai-input"
                    style={{
                      width: '100%',
                      background: 'rgba(255,255,255,0.08)',
                      border: '1px solid rgba(255,255,255,0.13)',
                      borderRadius: 8,
                      padding: '11px 14px',
                      color: '#fff',
                      fontSize: 15,
                      outline: 'none',
                      transition: 'box-shadow 0.15s',
                    }}
                  />
                </div>
              )}
              <div>
                <label htmlFor="sai-password" style={{ color: '#e5e7eb', fontSize: 14, fontWeight: 500, marginBottom: 6, display: 'block' }}>Password</label>
                <input
                  id="sai-password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder={setupRequired ? 'Min. 8 characters' : '••••••••'}
                  required
                  minLength={setupRequired ? 8 : 1}
                  className="sai-input"
                  style={{
                    width: '100%',
                    background: 'rgba(255,255,255,0.08)',
                    border: '1px solid rgba(255,255,255,0.13)',
                    borderRadius: 8,
                    padding: '11px 14px',
                    color: '#fff',
                    fontSize: 15,
                    outline: 'none',
                    transition: 'box-shadow 0.15s',
                  }}
                />
              </div>
              {error && (
                <div className="sai-error" style={{
                  padding: '12px 14px',
                  borderRadius: 10,
                  background: 'rgba(239,68,68,0.10)',
                  border: '1px solid rgba(239,68,68,0.22)',
                  color: '#f87171',
                  fontSize: 14,
                  marginBottom: 2,
                }}>
                  {error}
                </div>
              )}
              <button
                type="submit"
                disabled={loading}
                className="sai-primary-btn"
                style={{
                  width: '100%',
                  background: 'linear-gradient(135deg,#7c3aed,#06b6d4)',
                  color: '#fff',
                  fontWeight: 600,
                  border: 'none',
                  borderRadius: 999,
                  padding: '12px 0',
                  fontSize: 16,
                  marginTop: 2,
                  boxShadow: '0 2px 8px rgba(6,182,212,0.08)',
                  cursor: loading ? 'not-allowed' : 'pointer',
                  opacity: loading ? 0.6 : 1,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  gap: 10,
                  transition: 'background-position 0.2s',
                  backgroundSize: '200% 200%',
                  backgroundPosition: '0% 50%',
                }}
                onMouseOver={e => { (e.currentTarget as HTMLButtonElement).style.backgroundPosition = '100% 50%'; }}
                onMouseOut={e => { (e.currentTarget as HTMLButtonElement).style.backgroundPosition = '0% 50%'; }}
                aria-busy={loading}
              >
                {loading ? (
                  <>
                    <span className="sai-btn-spinner" style={{
                      width: 18, height: 18,
                      border: '2px solid rgba(255,255,255,0.3)',
                      borderTop: '2px solid #fff',
                      borderRadius: '50%',
                      display: 'inline-block',
                      animation: 'sai-spin 1s linear infinite',
                    }} />
                    {setupRequired ? 'Creating account…' : 'Signing in…'}
                  </>
                ) : (
                  setupRequired ? 'Create Admin Account' : 'Sign In'
                )}
              </button>
            </form>
            <div className="sai-footer" style={{ marginTop: 28, textAlign: 'center', color: '#7c8499', fontSize: 12, opacity: 0.65 }}>
              © SurgicalAI
            </div>
          </div>
        </div>
        <style>{`
          .sai-mesh-bg {
            position: absolute;
            inset: 0;
            z-index: 0;
            pointer-events: none;
            opacity: 0.4;
            background:
              radial-gradient(ellipse 40% 60% at 20% 30%, rgba(139,92,246,0.23) 0%, transparent 80%),
              radial-gradient(ellipse 30% 40% at 80% 70%, rgba(6,182,212,0.18) 0%, transparent 80%),
              radial-gradient(ellipse 25% 30% at 60% 20%, rgba(236,72,153,0.10) 0%, transparent 80%),
              radial-gradient(ellipse 20% 25% at 70% 80%, rgba(59,130,246,0.13) 0%, transparent 80%);
            animation: sai-meshShift 30s infinite alternate ease-in-out;
          }
          @keyframes sai-meshShift {
            0% {
              background-position:
                20% 30%,
                80% 70%,
                60% 20%,
                70% 80%;
              transform: scale(1);
            }
            100% {
              background-position:
                25% 35%,
                75% 65%,
                65% 25%,
                65% 85%;
              transform: scale(1.04);
            }
          }
          @media (prefers-reduced-motion: reduce) {
            .sai-mesh-bg { animation: none !important; }
            .sai-btn-spinner { animation: none !important; }
          }
          .sai-input:focus-visible {
            outline: 2px solid #06b6d4;
            outline-offset: 2px;
            box-shadow: 0 0 0 2px #06b6d4;
          }
          .sai-primary-btn:focus-visible {
            outline: 2px solid #8b5cf6;
            outline-offset: 2px;
            box-shadow: 0 0 0 2px #8b5cf6;
          }
          @supports not ((-webkit-backdrop-filter: blur(16px)) or (backdrop-filter: blur(16px))) {
            .sai-glass-card {
              background: rgba(17,25,40,0.7) !important;
            }
          }
          .sai-primary-btn:disabled {
            cursor: not-allowed;
            opacity: 0.6;
          }
        `}</style>
      </div>
    );
  }

  // DESIGN === 'B'
  return (
    <>
      <style>{`
        @keyframes lp-pulse-once {
          0% { transform: scale(1); opacity: 1; }
          40% { transform: scale(1.045); opacity: 1; }
          60% { transform: scale(1.02); opacity: 1; }
          100% { transform: scale(1); opacity: 1; }
        }
        .lp-pulse-once {
          animation-name: lp-pulse-once;
          animation-duration: 900ms;
          animation-timing-function: cubic-bezier(0.22, 1, 0.36, 1);
          animation-iteration-count: 1;
          animation-fill-mode: none;
          transform-origin: center;
          will-change: transform;
        }
        .lp-pulse-once--delay {
          animation-delay: 120ms;
        }
        @media (prefers-reduced-motion: reduce) {
          .lp-pulse-once, .lp-pulse-once--delay { animation: none !important; }
        }
      `}</style>
      <div className="sai-split-root" style={{ display: 'flex', minHeight: '100vh', width: '100vw', background: '#f7f8fb', position: 'relative', overflow: 'hidden' }}>
        <div className="sai-split-brand" style={{
          flex: 1,
          minWidth: 0,
          background: 'linear-gradient(145deg, #1b0b3a 0%, #2b0f63 100%)',
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'center',
          alignItems: 'flex-start',
          padding: '0 7vw',
          position: 'relative',
          color: '#fff',
          boxSizing: 'border-box',
        }}>
          <div className="sai-split-mesh" aria-hidden="true" />
          <div style={{ marginBottom: 36, marginTop: 12 }}>
            <span
              className="sai-logo lp-pulse-once"
              style={{
                fontWeight: 800,
                fontSize: 30,
                letterSpacing: '-0.04em',
                color: '#fff',
                textShadow: '0 2px 16px #8b5cf6, 0 1px 2px #0008',
                display: 'inline-block',
              }}
            >
              SurgicalAI
            </span>
          </div>
          <h1
            className="lp-pulse-once lp-pulse-once--delay"
            style={{
              fontSize: 32,
              fontWeight: 800,
              letterSpacing: '-0.03em',
              marginBottom: 12,
              lineHeight: 1.1,
              color: '#fff',
              textShadow: '0 2px 16px #8b5cf6, 0 1px 2px #0008',
            }}>
            Operate at the speed of thought
          </h1>
          <div style={{ fontSize: 17, color: '#e0e7ff', opacity: 0.92, marginBottom: 22, maxWidth: 420 }}>
            The world's safest autonomous code editor for teams.
          </div>
          <ul style={{ listStyle: 'none', padding: 0, margin: 0, color: '#e0e7ff', fontSize: 16, fontWeight: 500, lineHeight: 1.7, maxWidth: 420 }}>
            <li style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span style={{ color: '#22d3ee', fontSize: 18, marginRight: 4, display: 'inline-block' }}>
                <svg width="18" height="18" viewBox="0 0 20 20" fill="none"><circle cx="10" cy="10" r="10" fill="#22d3ee" opacity="0.18"/><path d="M6 10.5l2.5 2.5 5-5" stroke="#22d3ee" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
              </span>
              Autonomous code changes
            </li>
            <li style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span style={{ color: '#a78bfa', fontSize: 18, marginRight: 4, display: 'inline-block' }}>
                <svg width="18" height="18" viewBox="0 0 20 20" fill="none"><circle cx="10" cy="10" r="10" fill="#a78bfa" opacity="0.18"/><path d="M6 10.5l2.5 2.5 5-5" stroke="#a78bfa" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
              </span>
              Safe plan-and-surgery workflow
            </li>
            <li style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span style={{ color: '#f472b6', fontSize: 18, marginRight: 4, display: 'inline-block' }}>
                <svg width="18" height="18" viewBox="0 0 20 20" fill="none"><circle cx="10" cy="10" r="10" fill="#f472b6" opacity="0.18"/><path d="M6 10.5l2.5 2.5 5-5" stroke="#f472b6" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
              </span>
              Secure first-run admin setup
            </li>
            <li style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span style={{ color: '#818cf8', fontSize: 18, marginRight: 4, display: 'inline-block' }}>
                <svg width="18" height="18" viewBox="0 0 20 20" fill="none"><circle cx="10" cy="10" r="10" fill="#818cf8" opacity="0.18"/><path d="M6 10.5l2.5 2.5 5-5" stroke="#818cf8" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
              </span>
              Battle-tested diff/PR flow
            </li>
          </ul>
        </div>
        <div className="sai-split-form" style={{
          flex: 1,
          minWidth: 0,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: '#f7f8fb',
          padding: '0 2vw',
          boxSizing: 'border-box',
        }}>
          <div className="sai-split-card" style={{
            background: '#fff',
            borderRadius: 16,
            border: '1px solid rgba(0,0,0,0.06)',
            boxShadow: '0 10px 30px rgba(0,0,0,0.08)',
            width: '100%',
            maxWidth: 420,
            padding: '2.5rem 2rem 2rem 2rem',
            minWidth: 0,
          }}>
            <div className="sai-header" style={{ marginBottom: 32, textAlign: 'center' }}>
              <h2 style={{
                color: '#23263b',
                fontWeight: 700,
                fontSize: 22,
                margin: 0,
                letterSpacing: '-0.01em'
              }}>
                {setupRequired ? 'Set up your admin' : 'Sign in'}
              </h2>
              <div style={{ color: '#6b7280', fontSize: 14, marginTop: 4 }}>
                {setupRequired
                  ? 'Set up your admin credentials to begin'
                  : 'Sign in to your SurgicalAI workspace'}
              </div>
            </div>
            {setupRequired && (
              <div className="sai-firstrun-banner" style={{
                marginBottom: 24,
                padding: '12px 14px',
                borderRadius: 10,
                background: 'rgba(139,92,246,0.10)',
                border: '1px solid rgba(139,92,246,0.22)',
                textAlign: 'left'
              }}>
                <div style={{ color: '#8b5cf6', fontWeight: 600, fontSize: 13 }}>First time setup</div>
                <div style={{ color: '#8b5cf6', opacity: 0.7, fontSize: 12, marginTop: 2 }}>
                  This account will have admin access to manage users.
                </div>
              </div>
            )}
            <form onSubmit={handleSubmit} className="sai-form" style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
              <div>
                <label htmlFor="sai-username" style={{ color: '#23263b', fontSize: 14, fontWeight: 500, marginBottom: 6, display: 'block' }}>Username</label>
                <input
                  id="sai-username"
                  type="text"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="your-username"
                  required
                  minLength={3}
                  className="sai-input"
                  style={{
                    width: '100%',
                    background: '#fff',
                    border: '1px solid #e5e7eb',
                    borderRadius: 8,
                    padding: '11px 14px',
                    color: '#23263b',
                    fontSize: 15,
                    outline: 'none',
                    transition: 'box-shadow 0.15s',
                  }}
                />
              </div>
              {setupRequired && (
                <div>
                  <label htmlFor="sai-email" style={{ color: '#23263b', fontSize: 14, fontWeight: 500, marginBottom: 6, display: 'block' }}>
                    Email <span style={{ color: '#6b7280', fontWeight: 400 }}>(optional)</span>
                  </label>
                  <input
                    id="sai-email"
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="you@example.com"
                    className="sai-input"
                    style={{
                      width: '100%',
                      background: '#fff',
                      border: '1px solid #e5e7eb',
                      borderRadius: 8,
                      padding: '11px 14px',
                      color: '#23263b',
                      fontSize: 15,
                      outline: 'none',
                      transition: 'box-shadow 0.15s',
                    }}
                  />
                </div>
              )}
              <div>
                <label htmlFor="sai-password" style={{ color: '#23263b', fontSize: 14, fontWeight: 500, marginBottom: 6, display: 'block' }}>Password</label>
                <input
                  id="sai-password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder={setupRequired ? 'Min. 8 characters' : '••••••••'}
                  required
                  minLength={setupRequired ? 8 : 1}
                  className="sai-input"
                  style={{
                    width: '100%',
                    background: '#fff',
                    border: '1px solid #e5e7eb',
                    borderRadius: 8,
                    padding: '11px 14px',
                    color: '#23263b',
                    fontSize: 15,
                    outline: 'none',
                    transition: 'box-shadow 0.15s',
                  }}
                />
              </div>
              {error && (
                <div className="sai-error" style={{
                  padding: '12px 14px',
                  borderRadius: 10,
                  background: 'rgba(239,68,68,0.10)',
                  border: '1px solid rgba(239,68,68,0.22)',
                  color: '#ef4444',
                  fontSize: 14,
                  marginBottom: 2,
                }}>
                  {error}
                </div>
              )}
              <button
                type="submit"
                disabled={loading}
                className="sai-primary-btn"
                style={{
                  width: '100%',
                  background: 'linear-gradient(135deg,#7c3aed,#06b6d4)',
                  color: '#fff',
                  fontWeight: 600,
                  border: 'none',
                  borderRadius: 999,
                  padding: '12px 0',
                  fontSize: 16,
                  marginTop: 2,
                  boxShadow: '0 2px 8px rgba(6,182,212,0.08)',
                  cursor: loading ? 'not-allowed' : 'pointer',
                  opacity: loading ? 0.6 : 1,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  gap: 10,
                  transition: 'background-position 0.2s',
                  backgroundSize: '200% 200%',
                  backgroundPosition: '0% 50%',
                }}
                onMouseOver={e => { (e.currentTarget as HTMLButtonElement).style.backgroundPosition = '100% 50%'; }}
                onMouseOut={e => { (e.currentTarget as HTMLButtonElement).style.backgroundPosition = '0% 50%'; }}
                aria-busy={loading}
              >
                {loading ? (
                  <>
                    <span className="sai-btn-spinner" style={{
                      width: 18, height: 18,
                      border: '2px solid rgba(255,255,255,0.3)',
                      borderTop: '2px solid #fff',
                      borderRadius: '50%',
                      display: 'inline-block',
                      animation: 'sai-spin 1s linear infinite',
                    }} />
                    {setupRequired ? 'Creating account…' : 'Signing in…'}
                  </>
                ) : (
                  setupRequired ? 'Create Admin Account' : 'Sign In'
                )}
              </button>
            </form>
            <div className="sai-footer" style={{ marginTop: 28, textAlign: 'center', color: '#7c8499', fontSize: 12, opacity: 0.65 }}>
              © SurgicalAI
            </div>
          </div>
        </div>
        <style>{`
          .sai-split-root { min-height: 100vh; width: 100vw; }
          .sai-split-brand {
            position: relative;
            overflow: hidden;
          }
          .sai-split-mesh {
            position: absolute;
            inset: 0;
            z-index: 0;
            pointer-events: none;
            opacity: 0.22;
            background:
              radial-gradient(ellipse 40% 60% at 20% 30%, rgba(139,92,246,0.13) 0%, transparent 80%),
              radial-gradient(ellipse 30% 40% at 80% 70%, rgba(6,182,212,0.10) 0%, transparent 80%),
              radial-gradient(ellipse 25% 30% at 60% 20%, rgba(236,72,153,0.07) 0%, transparent 80%),
              radial-gradient(ellipse 20% 25% at 70% 80%, rgba(59,130,246,0.09) 0%, transparent 80%);
            animation: sai-meshShift 30s infinite alternate ease-in-out;
          }
          @keyframes sai-meshShift {
            0% {
              background-position:
                20% 30%,
                80% 70%,
                60% 20%,
                70% 80%;
              transform: scale(1);
            }
            100% {
              background-position:
                25% 35%,
                75% 65%,
                65% 25%,
                65% 85%;
              transform: scale(1.04);
            }
          }
          @media (max-width: 920px) {
            .sai-split-root {
              flex-direction: column;
            }
            .sai-split-brand {
              min-height: 40vh;
              padding-top: 36px;
              padding-bottom: 24px;
              align-items: center;
              text-align: center;
            }
            .sai-split-form {
              min-height: 60vh;
              padding-top: 32px;
              padding-bottom: 32px;
            }
          }
          @media (prefers-reduced-motion: reduce) {
            .sai-split-mesh { animation: none !important; }
            .sai-btn-spinner { animation: none !important; }
          }
          .sai-input:focus-visible {
            outline: 2px solid #7c3aed;
            outline-offset: 2px;
            box-shadow: 0 0 0 2px #7c3aed;
          }
          .sai-primary-btn:focus-visible {
            outline: 2px solid #8b5cf6;
            outline-offset: 2px;
            box-shadow: 0 0 0 2px #8b5cf6;
          }
          .sai-primary-btn:disabled {
            cursor: not-allowed;
            opacity: 0.6;
          }
          .sai-btn-spinner {
            animation: sai-spin 1s linear infinite;
          }
          @keyframes sai-spin { to { transform: rotate(360deg); } }
        `}</style>
      </div>
    </>
  );
}
