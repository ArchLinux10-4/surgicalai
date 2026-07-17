import { useState, useEffect } from 'react';
import {
  Box,
  Button,
  TextField,
  Typography,
  Link,
  Paper,
  InputAdornment,
  IconButton,
} from '@mui/material';
import { Visibility, VisibilityOff, CheckCircle } from '@mui/icons-material';
//import { useNavigate } from 'react-router-dom';
import { useNavigate } from 'react-router-dom';
import HealthIQLogoMark from './HealthIQLogoMark';
import TimelineIcon from '@mui/icons-material/Timeline';
import AutoFixHighIcon from '@mui/icons-material/AutoFixHigh';
import ForgotPassword from './ForgotPassword';
import ResetPassword from './ResetPassword';
import UpdateBilling from './UpdateBilling';
import AdminMfaPrompt from './AdminMfaPrompt';
import JobMarketTicker from './JobMarketTicker';
import { useTheme } from '@mui/material/styles';
import useMediaQuery from '@mui/material/useMediaQuery';
import RememberMePanel from './RememberMePanel';

const AuthGate = ({ children }) => {
  // =========================
  // Logo controls (tweak these)
  // =========================
  const LOGO_DELAY_MS = 800; // logo appears ~800ms after AuthGate mounts
  const LOGO_HEIGHT = 110; // resize logo
  const LOGO_ANIMATE_PILLS = true; // toggle pill animation on/off
  const LOGO_GAP_BELOW = { xs: '0.2rem', sm: '0.1rem' }; // spacing below logo
  
  // Master toggle for the sci-fi teleport / zoom animation.
  // Set this to false to turn off all zoom/overlay/ticker color effects everywhere.
  const ENABLE_TELEPORT_ANIMATION = false;

  const theme = useTheme();
  // Treat small screens as "mobile" for this effect; adjust breakpoint if you
  // also want to disable on tablets (e.g. theme.breakpoints.down('md')).
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'));

  // Only run teleport visuals on non-mobile when globally enabled.
  const teleportEnabled = ENABLE_TELEPORT_ANIMATION && !isMobile;

  // Teleport “tunnel” duration (must roughly match keyframe timing below).
  // On mobile or when disabled, this becomes 0 so navigation is immediate.
  const TELEPORT_DURATION_MS = teleportEnabled ? 1100 : 0;

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [showSupportEmail, setShowSupportEmail] = useState(false);
  const [rememberMe, setRememberMe] = useState(false);

  const [error, setError] = useState('');
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [showForgotPassword, setShowForgotPassword] = useState(false);

  const [isLoggingIn, setIsLoggingIn] = useState(false);
  const [loginSuccess, setLoginSuccess] = useState(false);
  const [loginErrorCode, setLoginErrorCode] = useState(null);

  // Trial expired upgrade state
  const [showUpgradePlans, setShowUpgradePlans] = useState(false);
  const [selectedUpgradePlan, setSelectedUpgradePlan] = useState('UNLIMITED_PRO');
  const [upgradeLoading, setUpgradeLoading] = useState(false);
  const [upgradeError, setUpgradeError] = useState('');

  // NEW: remember-me auto-login option (if available)
  const [autoLoginOption, setAutoLoginOption] = useState(null);
  const [autoLoginChecking, setAutoLoginChecking] = useState(false);
  
  // NEW: admin login step-up state
  const [adminMfaState, setAdminMfaState] = useState(null);
  
  // Detect ?upgrade_session_id= on page load (unauthenticated post-Stripe redirect for expired trial)
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const upgradeSessionId = params.get('upgrade_session_id');
    if (!upgradeSessionId || localStorage.getItem('sessionId')) return; // skip if already logged in

    (async () => {
      try {
        const res = await fetch('/api/auth/complete-trial-upgrade-public', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sessionId: upgradeSessionId }),
        });
        const data = await res.json();
        if (data.success && data.sessionId) {
          localStorage.setItem('sessionId', data.sessionId);
          // Clean the URL
          window.history.replaceState({}, '', '/');
          setIsAuthenticated(true);
          navigate('/dashboard');
        } else {
          setError(data.message || 'Upgrade could not be completed. Please contact support.');
        }
      } catch (err) {
        console.error('[TRIAL-UPGRADE-PUBLIC-COMPLETE]', err);
        setError('Error completing upgrade. Please try logging in or contact support.');
      }
    })();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // NEW: environment banner + maintenance mode + optional "back online" timestamp
  const [envBanner, setEnvBanner] = useState({
    color: 'green', // 'green' | 'red'
    message: 'Live environment',
    maintenance: false,
    maintenanceUntil: null,
  });
  const [envBannerLoading, setEnvBannerLoading] = useState(true);
  const [maintenanceCountdown, setMaintenanceCountdown] = useState(null);

  // Treat this username as admin for maintenance override
  const ADMIN_USERS = new Set(['pmolczan', 'admin', 'jg']);

  const isAdminUser =
    username && typeof username === 'string'
      ? ADMIN_USERS.has(username.toLowerCase().trim())
      : false;
  
  // Derived: is maintenance currently active for THIS user (non-admin)?
  const isMaintenanceActiveForUser = (() => {
    if (!envBanner.maintenance) return false;
    if (isAdminUser) return false;
  
    if (envBanner.maintenanceUntil) {
      const untilMs = new Date(envBanner.maintenanceUntil).getTime();
      if (!Number.isNaN(untilMs) && untilMs <= Date.now()) {
        // Countdown finished → treat as no longer in maintenance
        return false;
      }
    }
  
    return true;
  })();

  const navigate = useNavigate();

  // Delay showing the logo after the login screen mounts
  const [showLogo, setShowLogo] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setShowLogo(true), LOGO_DELAY_MS);
    return () => clearTimeout(t);
  }, []);

  useEffect(() => {
    const sessionId = localStorage.getItem('sessionId');
    if (sessionId) {
      fetch('/api/check-session', {
        headers: { 'x-session-id': sessionId },
      })
        .then((res) => res.json())
        .then((data) => {
          if (data?.isAuthorized) {
            setIsAuthenticated(true);
          } else {
            localStorage.removeItem('sessionId');
            setIsAuthenticated(false);
          }
        })
        .catch(() => {
          localStorage.removeItem('sessionId');
          setIsAuthenticated(false);
        });
    }
  }, []);
  
  useEffect(() => {
    const fetchEnvStatus = async () => {
      try {
        const res = await fetch('/api/env-status');
        if (!res.ok) {
          // Low-risk fallback – stay green / live
          console.error('fetchEnvStatus: non-OK status=', res.status);
          setEnvBanner((prev) => ({
            ...prev,
            maintenance: false,
            color: 'green',
            maintenanceUntil: null,
          }));
          return;
        }
        const data = await res.json();
        const rawMessage = (data?.message ?? 'Live environment').toString();
        const trimmedMessage = rawMessage.slice(0, 50);

        const normalizedColor = data?.color === 'red' ? 'red' : 'green';
        const maintenanceFlag =
          typeof data?.maintenance === 'boolean'
            ? data.maintenance
            : normalizedColor === 'red';

        const maintenanceUntil =
          data && data.maintenanceUntil ? String(data.maintenanceUntil) : null;

        setEnvBanner({
          color: normalizedColor,
          message: trimmedMessage || 'Live environment',
          maintenance: !!maintenanceFlag,
          maintenanceUntil,
        });
      } catch (err) {
        console.error('fetchEnvStatus error=', err);
        // On error, default to live/green so you don’t accidentally lock users out
        setEnvBanner((prev) => ({
          ...prev,
          maintenance: false,
          color: 'green',
          maintenanceUntil: null,
        }));
      } finally {
        setEnvBannerLoading(false);
      }
    };

    fetchEnvStatus();
  }, []);

  // NEW: probe remember-me cookie and prepare a "Continue as ..." option
  useEffect(() => {
    // Wait until env banner is loaded
    if (envBannerLoading) return;

    // Do not allow auto-login while global maintenance is active
    if (envBanner.maintenance) {
      setAutoLoginChecking(false);
      setAutoLoginOption(null);
      return;
    }

    // If we already have a live session, no need to probe
    if (isAuthenticated) {
      setAutoLoginChecking(false);
      setAutoLoginOption(null);
      return;
    }

    // If there's already a sessionId in localStorage, let the existing
    // /api/check-session flow decide what to do.
    const existingSessionId = localStorage.getItem('sessionId');
    if (existingSessionId) {
      setAutoLoginChecking(false);
      setAutoLoginOption(null);
      return;
    }

    const lastUsername = localStorage.getItem('piqLastUsername');
    if (!lastUsername) {
      setAutoLoginChecking(false);
      setAutoLoginOption(null);
      return;
    }

    let cancelled = false;
    setAutoLoginChecking(true);

    const probe = async () => {
      try {
        const res = await fetch('/api/auth/auto-login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include', // send HttpOnly remember-token cookie
        });

        let data = {};
        try {
          data = await res.json();
        } catch {
          data = {};
        }

        if (cancelled) return;

        if (res.ok && data && data.success && data.sessionId) {
          // We don't commit the session yet; wait for user to click "Continue"
          setAutoLoginOption({
            sessionId: data.sessionId,
            displayName: lastUsername,
          });
        } else if (data && data.errorCode === 'TRIAL_EXPIRED') {
          // Trial has expired — pre-fill username and show upgrade UI
          setAutoLoginOption(null);
          setUsername(lastUsername);
          setLoginErrorCode('TRIAL_EXPIRED');
          setError(data.message || 'Your 12-hour trial has ended. Upgrade to continue.');
        } else {
          setAutoLoginOption(null);
        }
      } catch (err) {
        if (!cancelled) {
          console.error('Auto-login probe failed:', err);
          setAutoLoginOption(null);
        }
      } finally {
        if (!cancelled) {
          setAutoLoginChecking(false);
        }
      }
    };

    probe();

    return () => {
      cancelled = true;
    };
  }, [envBannerLoading, envBanner.maintenance, isAuthenticated]);

  // NEW: live countdown label when maintenance + maintenanceUntil is set
  // When the timer hits zero, automatically flip the banner to "Live" on the client.
  useEffect(() => {
    let intervalId;

    if (envBanner.maintenance && envBanner.maintenanceUntil) {
      const targetMs = new Date(envBanner.maintenanceUntil).getTime();

      if (Number.isNaN(targetMs)) {
        setMaintenanceCountdown(null);
      } else {
        const updateCountdown = () => {
          const diff = targetMs - Date.now();

          if (diff <= 0) {
            // Timer finished → clear countdown and switch UI to Live
            setMaintenanceCountdown(null);
            setEnvBanner((prev) => {
              if (!prev.maintenance) return prev;
              return {
                ...prev,
                maintenance: false,
                color: 'green',
                maintenanceUntil: null,
                // Explicitly reset to the default live label
                message: 'Live environment',
              };
            });

            if (intervalId) {
              window.clearInterval(intervalId);
            }
            return;
          }

          const totalSeconds = Math.floor(diff / 1000);
          const hours = Math.floor(totalSeconds / 3600);
          const minutes = Math.floor((totalSeconds % 3600) / 60);
          const seconds = totalSeconds % 60;

          const label =
            hours > 0
              ? `${hours}h ${String(minutes).padStart(2, '0')}m ${String(
                  seconds
                ).padStart(2, '0')}s`
              : `${minutes}m ${String(seconds).padStart(2, '0')}s`;

          setMaintenanceCountdown(label);
        };

        updateCountdown();
        intervalId = window.setInterval(updateCountdown, 1000);
      }
    } else {
      setMaintenanceCountdown(null);
    }

    return () => {
      if (intervalId) {
        window.clearInterval(intervalId);
      }
    };
  }, [envBanner.maintenance, envBanner.maintenanceUntil]);

  const handleLogin = async (e) => {
    e.preventDefault();
    if (isLoggingIn || loginSuccess) return;

    // Only block NON-admins while maintenance is still active
    if (isMaintenanceActiveForUser) {
      setError(
        envBanner.message ||
          'The site is currently under maintenance. Please try again later.'
      );
      return;
    }

    if (!username || !password) {
      setError('Username and password are required.');
      return;
    }

    setIsLoggingIn(true);
    setLoginSuccess(false);
    setError('');
    setLoginErrorCode(null);
    setAdminMfaState(null);

    try {
      const response = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password, rememberMe }),
      });
      const data = await response.json();
      console.log('Login response:', data);

      setLoginErrorCode(data?.code || null);

      if (!response.ok) {
        setIsLoggingIn(false);
        setLoginSuccess(false);

        if (response.status === 404) {
          setError('Login service is unavailable. Please contact an administrator.');
        } else {
          setError(data?.message || 'Invalid username or password.');
        }
        return;
      }

      // NEW: admin step-up path
      if (data?.success && data?.step === 'ADMIN_MFA_REQUIRED' && data?.challengeId) {
        setIsLoggingIn(false);
        setLoginSuccess(false);
        setError('');

        setAdminMfaState({
          username: username,
          challengeId: data.challengeId,
          maskedEmail: data.maskedEmail || '',
          message:
            data.message ||
            'Additional verification is required before completing admin login.',
        });

        // Do NOT set isAuthenticated yet; that happens after MFA verify
        return;
      }

      // Normal case: server returned a sessionId directly
      if (data?.success && data?.sessionId) {
        localStorage.setItem('sessionId', data.sessionId);

        // Only persist the username for the "Continue as ..." auto-login UX
        // when the user explicitly opted in via the Remember Me checkbox.
        // This is display-only, but it also reflects that we sent
        // rememberMe=true to the server (which controls whether a persistent
        // remember-token cookie is issued at all).
        if (rememberMe && username && typeof username === 'string') {
          localStorage.setItem('piqLastUsername', username.trim());
        } else if (!rememberMe) {
          // Explicitly clear any previously remembered username so a stale
          // auto-login option can't resurface on this browser after an
          // un-checked login.
          localStorage.removeItem('piqLastUsername');
        }

        // Reset any previous auto-login option; we're now freshly logged in.
        setAutoLoginOption(null);
        setAutoLoginChecking(false);

        setError('');
        setIsLoggingIn(false);
        setLoginSuccess(true); // triggers green button + any teleport visuals

        // When teleport animation is enabled, use the full tunnel duration.
        // When it's disabled (or on mobile), still pause briefly so the green
        // success state is visible before routing.
        const delayMs = teleportEnabled ? TELEPORT_DURATION_MS : 700;

        setTimeout(() => {
          setIsAuthenticated(true);
          navigate('/dashboard');
        }, delayMs);

        return;
      }

      setIsLoggingIn(false);
      setLoginSuccess(false);
      setError(data?.message || 'Invalid username or password.');
    } catch (error) {
      console.error('Login fetch error:', error);
      setIsLoggingIn(false);
      setLoginSuccess(false);
      setLoginErrorCode(null);
      setError('Error connecting to server. Please try again or contact an administrator.');
    }
  };

  const handleUpgradeFromLogin = async () => {
    setUpgradeLoading(true);
    setUpgradeError('');
    try {
      const res = await fetch('/api/auth/create-trial-upgrade-checkout-public', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password, planKey: selectedUpgradePlan }),
      });
      const data = await res.json();
      if (data.url) {
        window.location.href = data.url;
      } else {
        setUpgradeError(data.message || 'Could not start checkout. Please try again.');
      }
    } catch (err) {
      setUpgradeError('Error connecting to server. Please try again.');
    } finally {
      setUpgradeLoading(false);
    }
  };

  const handleTogglePasswordVisibility = () => {
    setShowPassword((prev) => !prev);
  };

  const handleAutoLoginContinue = () => {
    if (!autoLoginOption || !autoLoginOption.sessionId) return;
    if (isLoggingIn || loginSuccess) return; // prevent double-invocation

    setIsLoggingIn(true); // block re-entry immediately, before any async work

    const { sessionId } = autoLoginOption;
    localStorage.setItem('sessionId', sessionId);
    setError('');
    setLoginSuccess(true); // reuse green success + teleport visuals

    const delayMs = teleportEnabled ? TELEPORT_DURATION_MS : 700;

    setTimeout(() => {
      setIsAuthenticated(true);
      navigate('/dashboard');
    }, delayMs);
  };

  if (isAuthenticated) {
    return <>{children}</>;
  }

  // If user visits the email reset link route, show ResetPassword screen
  if (window.location.pathname === '/reset-password') {
    return <ResetPassword onBackToLogin={() => navigate('/login')} />;
  }

  if (showForgotPassword) {
    return <ForgotPassword onBackToLogin={() => setShowForgotPassword(false)} />;
  }

  // NEW: Admin MFA step-up screen
  if (adminMfaState) {
    return (
      <AdminMfaPrompt
        state={adminMfaState}
        onCancel={() => {
          setAdminMfaState(null);
          setIsLoggingIn(false);
          setLoginSuccess(false);
          setError('');
        }}
        onComplete={(sessionId) => {
          if (sessionId) {
            localStorage.setItem('sessionId', sessionId);
          }
          setError('');
          setIsLoggingIn(false);
          setLoginSuccess(true); // reuse same teleport animation

          setTimeout(() => {
            setIsAuthenticated(true);
            navigate('/dashboard');
          }, TELEPORT_DURATION_MS);
        }}
      />
    );
  }

  return (
    <Box
      sx={{
        display: 'flex',
        // Desktop: centered. Mobile: natural scroll (prevents iPhone snap-y behavior)
        alignItems: { xs: 'stretch', md: 'center' },
        justifyContent: { xs: 'flex-start', md: 'center' },
        // iOS Safari: stable viewport height (prevents scroll snapping when address bar changes)
        minHeight: { xs: '100svh', md: '100dvh' },
        width: '100vw',
        margin: 0,
        padding: 0,
        // More bottom padding on iPhone + safe-area support
        px: { xs: '1.25rem', sm: '1.75rem', md: '2.5rem' },
        pt: { xs: '1.25rem', sm: '2rem', md: '2.5rem' },
        pb: {
          xs: 'calc(3.25rem + env(safe-area-inset-bottom))',
          sm: '2.75rem',
          md: '2.5rem',
        },
        overflowY: { xs: 'auto', md: 'visible' },
        WebkitOverflowScrolling: 'touch',
        overscrollBehaviorY: 'contain',
        overflowX: 'hidden',
        // Full-screen background
        background:
          'radial-gradient(circle at top, rgba(41,192,219,0.15), rgba(148,163,184,0.12) 35%, #f4f4f4 80%)',
        backgroundColor: '#f4f4f4',
        boxSizing: 'border-box',
      }}
    >
      {/* TELEPORT OVERLAY – plays during successful login (desktop only, if enabled) */}
      {teleportEnabled && loginSuccess && (
        <Box
          sx={{
            position: 'fixed',
            inset: 0,
            zIndex: 1300,
            pointerEvents: 'none',
            opacity: 0,
            background:
              'radial-gradient(circle at 50% 15%, rgba(41,192,219,0.32), transparent 55%), ' +
              'radial-gradient(circle at 50% 60%, rgba(15,23,42,0.98), #020617 78%)',
            '@keyframes tickerTeleportFade': {
              '0%': { opacity: 0 },
              '30%': { opacity: 0.35 },
              '65%': { opacity: 0.9 },
              '100%': { opacity: 1 },
            },
            animation: 'tickerTeleportFade 1100ms ease-in forwards',
          }}
        >
          {/* Tunnel rings */}
          <Box
            sx={{
              position: 'absolute',
              top: '50%',
              left: '50%',
              width: '260vw',
              height: '260vh',
              transform: 'translate(-50%, -50%)',
              backgroundImage:
                'radial-gradient(circle, transparent 0, transparent 40%, rgba(15,23,42,0.95) 41%, rgba(15,23,42,1) 100%)',
              maskImage:
                'radial-gradient(circle at 50% 50%, transparent 0, transparent 30%, black 46%, black 100%)',
              opacity: 0.85,
              '@keyframes tickerTeleportRings': {
                '0%': {
                  transform: 'translate(-50%, -50%) scale(0.65)',
                  opacity: 0.55,
                },
                '55%': {
                  transform: 'translate(-50%, -50%) scale(1.05)',
                  opacity: 0.9,
                },
                '100%': {
                  transform: 'translate(-50%, -50%) scale(1.2)',
                  opacity: 1,
                },
              },
              animation:
                'tickerTeleportRings 1100ms cubic-bezier(0.22, 0.64, 0.21, 1) forwards',
            }}
          />

          {/* Horizontal sci-fi “scan” lines */}
          <Box
            sx={{
              position: 'absolute',
              inset: '-10%',
              backgroundImage:
                'repeating-linear-gradient(90deg, rgba(148,163,184,0.06) 0, rgba(148,163,184,0.06) 1px, transparent 2px, transparent 12px)',
              mixBlendMode: 'screen',
              opacity: 0.35,
              '@keyframes tickerTeleportStrafe': {
                '0%': { transform: 'translateX(0)' },
                '100%': { transform: 'translateX(-60px)' },
              },
              animation: 'tickerTeleportStrafe 1100ms linear forwards',
            }}
          />
        </Box>
      )}

      <Box
        sx={{
          width: '100%',
          maxWidth: 1100,
          display: 'flex',
          flexDirection: 'column',
          gap: { xs: '1rem', md: '1.5rem' },
          // ensure centered block on desktop
          mx: 'auto',

          // NEW: global zoom for the entire auth card when login succeeds
          // Anchor near the top so it feels like we're zooming into the ticker/login strip.
          transformOrigin: {
            xs: '50% 5%',
            md: '50% 5%',
          },
          transition: 'transform 1100ms cubic-bezier(0.19, 1, 0.22, 1)',
          transform: teleportEnabled && loginSuccess ? 'scale(5.33)' : 'scale(1)',
        }}
      >
        {/* TOP WHITE HEADER
            Mobile: keep exactly the simple centered logo behavior.
            Desktop: redesigned with reassurance/feature cards to avoid the "lonely logo" look.
        */}
        <Paper
          elevation={2}
          sx={{
            width: '100%',
            // top-left, top-right, bottom-right, bottom-left
            borderRadius: {
              xs: '9px 9px 18px 18px',
              md: '12px 12px 22px 22px',
            },
            backgroundColor: '#ffffff',
            boxShadow: '0 14px 35px rgba(15,23,42,0.10)',
            padding: { xs: '1rem 1rem', sm: '1.1rem 1.25rem', md: '1.1rem 1.5rem' },
            boxSizing: 'border-box',
            overflow: 'hidden',
            position: 'relative',
          }}
        >
          {/* NEW: Job market ticker replaces previous gradient accent line */}
          <Box
            sx={{
              mb: { xs: 1, md: 1.25 },
              // Make ticker span full width of the white container (cancel Paper padding)
              mx: {
                xs: '-1.2rem',    // matches xs padding in Paper
                sm: '-1.25rem',   // matches sm padding in Paper
                md: '-1.6rem',    // matches md padding in Paper
              },
              // Pull the ticker up slightly so it visually lines up with the top edge
              mt: {
                xs: '-1.00rem',
                sm: '-1.1rem',
                md: '-1.1rem',
              },
              // TELEPORT GLOW when login succeeds – scaling happens on the parent container now
              transition: 'filter 900ms ease, box-shadow 900ms ease',
              filter:
                teleportEnabled && loginSuccess ? 'brightness(1.15) saturate(1.1)' : 'none',
              boxShadow:
                teleportEnabled && loginSuccess
                  ? '0 0 20px rgba(41,192,219,0.65), 0 0 60px rgba(15,23,42,0.95)'
                  : 'none',
            }}
          >
            <JobMarketTicker />
          </Box>

          <Box
            sx={{
              display: 'flex',
              alignItems: 'center',
              // Mobile stays centered (your current working mobile look).
              justifyContent: { xs: 'center', sm: 'flex-start', md: 'space-between' },
              gap: { xs: 1.25, md: 2.25 },
              flexWrap: 'wrap',
            }}
          >
            {/* Delayed logo (kept OUT of ticker window) */}
            <Box
              sx={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: { xs: 'center', sm: 'flex-start' },
                justifyContent: 'center',
                marginBottom: LOGO_GAP_BELOW,
                // reserve space so layout doesn't jump while logo is delayed
                minHeight: { xs: `${LOGO_HEIGHT + 22}px`, sm: `${LOGO_HEIGHT + 26}px` },
                flex: { xs: '0 0 auto', md: '0 0 auto' },
              }}
            >
              {showLogo && (
                <HealthIQLogoMark height={LOGO_HEIGHT} animatePills={LOGO_ANIMATE_PILLS} />
              )}
            </Box>

            {/* Desktop-only: feature cards (unchanged) */}
            <Box
              sx={{
                display: { xs: 'none', md: 'flex' },
                alignItems: 'stretch',
                gap: 1,
                flex: '1 1 auto',
                justifyContent: 'flex-end',
                minWidth: 420,
                flexWrap: 'wrap',
              }}
            >
              {/* Card 1 */}
              <Box
                sx={{
                  flex: '0 0 auto',
                  borderRadius: '14px',
                  padding: '0.85rem 1rem',
                  border: '1px solid rgba(148,163,184,0.55)',
                  background:
                    'linear-gradient(135deg, rgba(248,250,252,0.98), rgba(241,245,249,0.96))',
                  boxShadow: '0 10px 25px rgba(15,23,42,0.06)',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 1,
                }}
              >
                <AutoFixHighIcon sx={{ fontSize: '1.55rem', color: '#29c0db' }} />
                <Box>
                  <Typography sx={{ fontSize: '0.82rem', fontWeight: 800, color: '#0f172a' }}>
                    Search Automation
                  </Typography>
                  <Typography sx={{ fontSize: '0.78rem', color: '#64748b', lineHeight: 1.2 }}>
                    AI/ML-powered matching and automation
                  </Typography>
                </Box>
              </Box>

              {/* Card 2 */}
              <Box
                sx={{
                  flex: '0 0 auto',
                  borderRadius: '14px',
                  padding: '0.85rem 1rem',
                  border: '1px solid rgba(148,163,184,0.55)',
                  background:
                    'linear-gradient(135deg, rgba(248,250,252,0.98), rgba(241,245,249,0.96))',
                  boxShadow: '0 10px 25px rgba(15,23,42,0.06)',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 1,
                }}
              >
                <TimelineIcon sx={{ fontSize: '1.75rem', color: '#29c0db' }} />
                <Box>
                  <Typography sx={{ fontSize: '0.82rem', fontWeight: 800, color: '#0f172a' }}>
                    Market Trending
                  </Typography>
                  <Typography sx={{ fontSize: '0.78rem', color: '#64748b', lineHeight: 1.2 }}>
                    Monthly Trending + Predictive Analytics
                  </Typography>
                </Box>
              </Box>
            </Box>
          </Box>
        </Paper>

        {/* MAIN TWO-PANEL WINDOW */}
        <Box
          sx={{
            width: '100%',
            display: 'flex',
            flexDirection: { xs: 'column', md: 'row' },
            gap: { xs: '1.25rem', md: '2.25rem' },
            alignItems: 'stretch',
          }}
        >
          {/* Left info / branding panel (NO LOGO INSIDE HERE) */}
          <Box
            sx={{
              flexBasis: { xs: 'auto', md: '45%' },
              display: 'flex',
              flexDirection: 'column',
              justifyContent: 'space-between',
              borderRadius: { xs: '18px', md: '24px' },
              padding: { xs: '1.5rem 1.4rem', sm: '1.75rem', md: '2.1rem' },
              background: 'linear-gradient(135deg, #0f172a 0%, #1e293b 40%, #29c0db 100%)',
              color: '#f9fafb',
              boxShadow: '0 18px 45px rgba(15,23,42,0.55)',
              position: 'relative',
              overflow: 'hidden',
              mb: { xs: '0.35rem', md: 0 },
            }}
          >
            <Box sx={{ position: 'absolute', inset: 0, opacity: 0.12, pointerEvents: 'none' }}>
              <Box
                sx={{
                  position: 'absolute',
                  width: 220,
                  height: 220,
                  borderRadius: '50%',
                  border: '1px solid rgba(148,163,184,0.35)',
                  top: -40,
                  right: -40,
                }}
              />
              <Box
                sx={{
                  position: 'absolute',
                  width: 320,
                  height: 320,
                  borderRadius: '50%',
                  border: '1px solid rgba(148,163,184,0.25)',
                  bottom: -80,
                  left: -60,
                }}
              />
            </Box>

            <Box sx={{ position: 'relative', zIndex: 1 }}>
              <Typography
                variant="subtitle2"
                sx={{
                  textTransform: 'uppercase',
                  letterSpacing: 1.5,
                  fontWeight: 600,
                  mb: 1,
                  color: 'rgba(226,232,240,0.9)',
                }}
              >
                MarketRates.AI Analytics
              </Typography>

              <Typography
                variant="h4"
                sx={{
                  fontWeight: 700,
                  lineHeight: 1.15,
                  mb: 1.25,
                  fontSize: { xs: '1.8rem', md: '2.05rem' },
                }}
              >
                Welcome back.
                <br />
                Let’s get you in.
              </Typography>

              <Typography
                variant="body2"
                sx={{
                  maxWidth: 360,
                  color: 'rgba(226,232,240,0.92)',
                  mb: 2.25,
                }}
              >
                Log in to view live Rate Cards, Batch Automation results, and your latest platform
                notifications.
              </Typography>

              <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                  <CheckCircle sx={{ fontSize: '1.1rem', color: '#a5f3fc' }} />
                  <Typography variant="body2" sx={{ color: 'rgba(226,232,240,0.95)' }}>
                    Secure, reliable access — built to protect your workflow and data.
                  </Typography>
                </Box>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                  <CheckCircle sx={{ fontSize: '1.1rem', color: '#a5f3fc' }} />
                  <Typography variant="body2" sx={{ color: 'rgba(226,232,240,0.95)' }}>
                    Fast access to dashboards, rate cards, and chat support.
                  </Typography>
                </Box>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                  <CheckCircle sx={{ fontSize: '1.1rem', color: '#a5f3fc' }} />
                  <Typography variant="body2" sx={{ color: 'rgba(226,232,240,0.95)' }}>
                    Specialized Taxonomy for Nursing, Allied and Non-Clinical Data.
                  </Typography>
                </Box>
              </Box>
            </Box>

            {/* Bottom helper + LIVE ENV pill (kept exactly where you wanted it) */}
            <Box
              sx={{
                position: 'relative',
                zIndex: 1,
                mt: { xs: 2.25, md: 3.25 },
                display: 'flex',
                flexDirection: 'column',
                gap: 1.15,
              }}
            >
              <Box>
                <Typography
                  variant="caption"
                  sx={{ textTransform: 'uppercase', letterSpacing: 1, opacity: 0.8 }}
                >
                  Need help?
                </Typography>
                <Typography variant="body2" sx={{ fontWeight: 500 }}>
                  Use “Forgot password” or contact support{' '}
                  {!showSupportEmail ? (
                    <Link
                      component="button"
                      onClick={() => setShowSupportEmail(true)}
                      sx={{
                        color: 'rgba(226,232,240,0.95)',
                        textDecoration: 'underline',
                        fontWeight: 700,
                        cursor: 'pointer',
                      }}
                    >
                      show email
                    </Link>
                  ) : (
                    <Box component="span" sx={{ fontWeight: 800 }}>
                      info@heathiq.app{' '}
                      <Link
                        component="button"
                        onClick={async () => {
                          try {
                            await navigator.clipboard.writeText('info@heathiq.app');
                          } catch (e) {
                            // ignore
                          }
                        }}
                        sx={{
                          ml: 1,
                          color: '#a5f3fc',
                          textDecoration: 'underline',
                          fontWeight: 700,
                          cursor: 'pointer',
                          fontSize: '0.92em',
                        }}
                      >
                        copy
                      </Link>
                    </Box>
                  )}
                </Typography>
              </Box>

              {/* Environment pill (admin-configurable) at the very bottom inside the window */}
              <Box
                sx={{
                  mt: 0.25,
                  width: '100%',
                  display: 'flex',
                  flexDirection: 'column',
                  justifyContent: { xs: 'center', md: 'flex-start' },
                  alignItems: { xs: 'center', md: 'flex-start' },
                  gap: 0.35,
                }}
              >
                <Box
                  sx={{
                    padding: '0.45rem 0.85rem',
                    borderRadius: '999px',
                    border: '1px solid rgba(148,163,184,0.55)',
                    background:
                      envBanner.maintenance && !envBannerLoading
                        ? 'rgba(248,113,113,0.18)'
                        : 'rgba(255,255,255,0.10)',
                    color: 'rgba(226,232,240,0.95)',
                    fontSize: '0.78rem',
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: 0.55,
                    fontWeight: 600,
                    backdropFilter: 'blur(6px)',
                  }}
                >
                  <Box
                    sx={{
                      width: 9,
                      height: 9,
                      borderRadius: '50%',
                      backgroundColor:
                        envBanner.color === 'red' ? '#ef4444' : '#22c55e',
                      boxShadow:
                        envBanner.color === 'red'
                          ? '0 0 0 4px rgba(248,113,113,0.3)'
                          : '0 0 0 4px rgba(34,197,94,0.16)',
                    }}
                  />
                  {envBannerLoading
                    ? 'Checking environment…'
                    : envBanner.message || 'Live environment'}
                </Box>

                {envBanner.maintenance && !envBannerLoading && (
                  <>
                    <Typography
                      variant="caption"
                      sx={{
                        color: 'rgba(248,250,252,0.9)',
                        maxWidth: 360,
                      }}
                    >
                      The site is currently under maintenance. Login is temporarily
                      disabled.
                    </Typography>

                    {maintenanceCountdown && (
                      <Box
                        sx={{
                          mt: 0.35,
                          px: 0.7,
                          py: 0.35,
                          borderRadius: '999px',
                          border: '1px solid rgba(148,163,184,0.55)',
                          backgroundColor: 'rgba(15,23,42,0.45)',
                          display: 'inline-flex',
                          alignItems: 'center',
                          gap: 0.45,
                        }}
                      >
                        <Typography
                          variant="caption"
                          sx={{
                            color: 'rgba(248,250,252,0.92)',
                            fontWeight: 500,
                          }}
                        >
                          Back online in
                        </Typography>
                        <Typography
                          variant="caption"
                          sx={{
                            color: '#e5e7eb',
                            fontWeight: 700,
                            letterSpacing: 0.5,
                          }}
                        >
                          {maintenanceCountdown}
                        </Typography>
                      </Box>
                    )}
                  </>
                )}
              </Box>
            </Box>
          </Box>

          {/* Right login card */}
          <Paper
            component="form"
            onSubmit={handleLogin}
            elevation={3}
            autoComplete="on"
            sx={{
              flexBasis: { xs: 'auto', md: '55%' },
              alignSelf: { xs: 'stretch', md: 'flex-start' },
              borderRadius: { xs: '18px', md: '24px' },
              padding: { xs: '1.5rem 1.25rem', sm: '1.75rem', md: '2.15rem' },
              backgroundColor: '#ffffff',
              boxShadow: '0 18px 40px rgba(15,23,42,0.16)',
              display: 'flex',
              flexDirection: 'column',
              gap: '0.95rem',
            }}
          >
            <Box sx={{ mb: 0.25 }}>
              <Typography
                variant="h5"
                sx={{
                  fontWeight: 700,
                  color: '#0f172a',
                  fontSize: { xs: '1.35rem', md: '1.6rem' },
                  mb: 0.3,
                }}
              >
                Log in
              </Typography>
              <Typography variant="body2" sx={{ color: '#64748b', fontSize: '0.85rem' }}>
                Enter your credentials to continue.
              </Typography>
            </Box>

            {autoLoginOption &&
              autoLoginOption.displayName &&
              !isMaintenanceActiveForUser && (
                <RememberMePanel
                  displayName={autoLoginOption.displayName}
                  disabled={isLoggingIn || loginSuccess}
                  onContinue={handleAutoLoginContinue}
                  onUseDifferentAccount={() => {
                    // User chose to ignore the saved session and log in normally
                    setAutoLoginOption(null);
                    setAutoLoginChecking(false);
                  }}
                />
              )}

            {(!autoLoginOption ||
              !autoLoginOption.displayName ||
              isMaintenanceActiveForUser) && (
              <>
                <TextField
                  label="Username"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  variant="outlined"
                  fullWidth
                  required
                  autoComplete="username"
                  sx={{ '& .MuiInputBase-root': { backgroundColor: '#fff' } }}
                />

                <TextField
                  label="Password"
                  type={showPassword ? 'text' : 'password'}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  variant="outlined"
                  fullWidth
                  required
                  autoComplete="current-password"
                  sx={{ '& .MuiInputBase-root': { backgroundColor: '#fff' } }}
                  InputProps={{
                    endAdornment: (
                      <InputAdornment position="end">
                        <IconButton
                          aria-label="toggle password visibility"
                          onClick={handleTogglePasswordVisibility}
                          edge="end"
                        >
                          {showPassword ? <VisibilityOff /> : <Visibility />}
                        </IconButton>
                      </InputAdornment>
                    ),
                  }}
                />

                <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, mt: -0.25 }}>
                  <input
                    type="checkbox"
                    id="rememberMeCheckbox"
                    checked={rememberMe}
                    onChange={(e) => setRememberMe(e.target.checked)}
                    style={{ width: 16, height: 16, cursor: 'pointer' }}
                  />
                  <label
                    htmlFor="rememberMeCheckbox"
                    style={{ fontSize: '0.85rem', color: '#475569', cursor: 'pointer' }}
                  >
                    Remember me on this device
                  </label>
                </Box>
              </>
            )}

            {error && (
              <Box
                sx={{
                  padding: '0.65rem 0.75rem',
                  borderRadius: '12px',
                  border: '1px solid rgba(239,68,68,0.35)',
                  background: 'rgba(239,68,68,0.06)',
                }}
              >
                <Typography variant="body2" sx={{ color: '#b91c1c', fontWeight: 600, mb: 0.15 }}>
                  Login failed
                </Typography>
                <Typography variant="body2" sx={{ color: '#7f1d1d', fontSize: '0.86rem' }}>
                  {error}
                </Typography>

                {loginErrorCode === 'SUBSCRIPTION_INACTIVE' && (
                  <UpdateBilling username={username} password={password} />
                )}

                {loginErrorCode === 'TRIAL_EXPIRED' && (
                  <Box sx={{ mt: 1.5, borderTop: '1px solid rgba(239,68,68,0.2)', pt: 1.5 }}>
                    {!showUpgradePlans ? (
                      <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                        <Typography sx={{ fontSize: '0.78rem', color: '#7f1d1d', lineHeight: 1.4 }}>
                          Your <strong>$400 trial payment</strong> will be credited to your first month.
                        </Typography>
                        <Button
                          onClick={() => setShowUpgradePlans(true)}
                          size="small"
                          sx={{
                            ml: 1.5,
                            textTransform: 'none',
                            fontSize: '0.78rem',
                            px: 1.6,
                            py: 0.5,
                            borderRadius: '999px',
                            backgroundColor: '#29c0db',
                            color: '#fff',
                            boxShadow: 'none',
                            flexShrink: 0,
                            '&:hover': { backgroundColor: '#1aa8c1', boxShadow: 'none' },
                          }}
                        >
                          Upgrade Plan →
                        </Button>
                      </Box>
                    ) : (
                      <Box>
                        <Typography sx={{ fontSize: '0.75rem', color: '#7f1d1d', mb: 1, fontWeight: 600 }}>
                          Choose a plan to continue:
                        </Typography>

                        {[
                          { key: 'UNLIMITED_PRO',             label: 'Standard',      price: '$4,000/mo', sub: 'Monthly' },
                          { key: 'UNLIMITED_PRO_ANNUAL',      label: 'Standard',      price: '$3,600/mo', sub: 'Annual · 10% off', annual: true },
                          { key: 'UNLIMITED_PRO_PLUS',        label: 'Professional',  price: '$8,000/mo', sub: 'Monthly' },
                          { key: 'UNLIMITED_PRO_PLUS_ANNUAL', label: 'Professional',  price: '$7,200/mo', sub: 'Annual · 10% off', annual: true },
                        ].map((plan) => (
                          <Box
                            key={plan.key}
                            onClick={() => setSelectedUpgradePlan(plan.key)}
                            sx={{
                              display: 'flex',
                              alignItems: 'center',
                              justifyContent: 'space-between',
                              px: 1.4,
                              py: 0.9,
                              mb: 0.75,
                              borderRadius: '10px',
                              border: `1.5px solid ${selectedUpgradePlan === plan.key ? '#29c0db' : 'rgba(148,163,184,0.35)'}`,
                              backgroundColor: selectedUpgradePlan === plan.key ? 'rgba(41,192,219,0.07)' : '#fafafa',
                              cursor: 'pointer',
                              transition: 'all 0.15s',
                            }}
                          >
                            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                              <Box
                                sx={{
                                  width: 14, height: 14, borderRadius: '50%',
                                  border: `2px solid ${selectedUpgradePlan === plan.key ? '#29c0db' : '#cbd5e1'}`,
                                  backgroundColor: selectedUpgradePlan === plan.key ? '#29c0db' : 'transparent',
                                  flexShrink: 0,
                                }}
                              />
                              <Box>
                                <Typography sx={{ fontSize: '0.78rem', fontWeight: 600, color: '#0f172a', lineHeight: 1.2 }}>
                                  {plan.label}
                                </Typography>
                                <Typography sx={{ fontSize: '0.68rem', color: '#6b7280', lineHeight: 1.2 }}>
                                  {plan.sub}
                                  {plan.annual && (
                                    <Box component="span" sx={{ ml: 0.5, px: 0.5, py: 0.1, backgroundColor: '#dcfce7', color: '#15803d', borderRadius: '4px', fontSize: '0.6rem', fontWeight: 700 }}>
                                      10% OFF
                                    </Box>
                                  )}
                                </Typography>
                              </Box>
                            </Box>
                            <Typography sx={{ fontSize: '0.78rem', fontWeight: 700, color: '#0f172a' }}>
                              {plan.price}
                            </Typography>
                          </Box>
                        ))}

                        <Typography sx={{ fontSize: '0.68rem', color: '#29c0db', fontWeight: 600, mb: 1, textAlign: 'center' }}>
                          $400 trial credit auto-applied at checkout
                        </Typography>

                        {upgradeError && (
                          <Typography sx={{ fontSize: '0.72rem', color: '#ef4444', mb: 1 }}>{upgradeError}</Typography>
                        )}

                        <Box sx={{ display: 'flex', gap: 1 }}>
                          <Button
                            onClick={handleUpgradeFromLogin}
                            disabled={upgradeLoading}
                            size="small"
                            sx={{
                              textTransform: 'none',
                              fontSize: '0.78rem',
                              px: 1.6,
                              py: 0.55,
                              borderRadius: '999px',
                              backgroundColor: '#29c0db',
                              color: '#fff',
                              boxShadow: 'none',
                              flex: 1,
                              '&:hover': { backgroundColor: '#1aa8c1', boxShadow: 'none' },
                              '&.Mui-disabled': { backgroundColor: '#29c0db !important', color: '#fff !important', opacity: 0.7 },
                            }}
                          >
                            {upgradeLoading ? 'Redirecting…' : 'Proceed to Checkout →'}
                          </Button>
                          <Button
                            onClick={() => setShowUpgradePlans(false)}
                            size="small"
                            sx={{
                              textTransform: 'none',
                              fontSize: '0.78rem',
                              px: 1.4,
                              py: 0.55,
                              borderRadius: '999px',
                              border: '1.5px solid rgba(148,163,184,0.5)',
                              color: '#64748b',
                              backgroundColor: 'transparent',
                              boxShadow: 'none',
                              '&:hover': { backgroundColor: 'rgba(148,163,184,0.08)', boxShadow: 'none' },
                            }}
                          >
                            Back
                          </Button>
                        </Box>
                      </Box>
                    )}
                  </Box>
                )}
              </Box>
            )}

            {(!autoLoginOption ||
              !autoLoginOption.displayName ||
              isMaintenanceActiveForUser) && (
              <Button
                type="submit"
                variant="contained"
                fullWidth
                disabled={isLoggingIn || loginSuccess || isMaintenanceActiveForUser}
                startIcon={
                  loginSuccess ? <CheckCircle sx={{ fontSize: '1.2rem' }} /> : null
                }
                sx={{
                  mt: 0.25,
                  backgroundColor: loginSuccess
                    ? '#22c55e'
                    : envBanner.maintenance && !isAdminUser
                    ? '#9ca3af'
                    : '#29c0db',
                  color: '#fff',
                  textTransform: 'none',
                  padding: '0.6rem 0.75rem',
                  fontSize: '0.95rem',
                  borderRadius: '9px',
                  fontWeight: 600,
                  boxShadow: loginSuccess
                    ? '0 14px 35px rgba(34,197,94,0.45)'
                    : isMaintenanceActiveForUser
                    ? '0 8px 20px rgba(148,163,184,0.35)'
                    : '0 14px 35px rgba(41,192,219,0.6)',
                  transition: 'background-color 220ms ease, box-shadow 220ms ease',
                  '&:hover': {
                    backgroundColor: loginSuccess
                      ? '#16a34a'
                      : envBanner.maintenance && !isAdminUser
                      ? '#9ca3af'
                      : '#1aa8c1',
                    boxShadow: loginSuccess
                      ? '0 14px 35px rgba(34,197,94,0.45)'
                      : envBanner.maintenance && !isAdminUser
                      ? '0 8px 20px rgba(148,163,184,0.35)'
                      : '0 14px 35px rgba(41,192,219,0.6)',
                  },
                  // keep your disabled style override
                  '&.Mui-disabled': {
                    backgroundColor: loginSuccess
                      ? '#22c55e !important'
                      : isMaintenanceActiveForUser
                      ? '#9ca3af !important'
                      : '#29c0db !important',
                    color: '#fff !important',
                    opacity: 0.92,
                  },
                }}
              >
                {isMaintenanceActiveForUser
                  ? 'Maintenance mode'
                  : loginSuccess
                  ? 'Success'
                  : isLoggingIn
                  ? 'Logging in…'
                  : 'Login'}
              </Button>
            )}

            <Typography
              variant="body2"
              sx={{
                color: '#475569',
                fontWeight: 400,
                mt: 0.2,
                textAlign: 'center',
              }}
            >
              <Link
                component="button"
                onClick={() => setShowForgotPassword(true)}
                sx={{
                  color: '#29c0db',
                  textDecoration: 'none',
                  fontWeight: 600,
                  '&:hover': { textDecoration: 'underline' },
                }}
              >
                Forgot password?
              </Link>
            </Typography>

            <Typography
              variant="body2"
              sx={{
                color: '#475569',
                fontWeight: 400,
                mt: 0.25,
                textAlign: 'center',
              }}
            >
              Don’t have an account?{' '}
              <Link
                component="button"
                onClick={() => navigate('/signup')}
                sx={{
                  color: '#0f172a',
                  fontWeight: 700,
                  textDecoration: 'none',
                  '&:hover': { textDecoration: 'underline', color: '#1e293b' },
                }}
              >
                Sign Up
              </Link>
            </Typography>
          </Paper>
        </Box>
      </Box>
    </Box>
  );
};

export default AuthGate;
