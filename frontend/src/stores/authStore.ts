/**
 * Auth store — JWT token, current user, login/logout.
 * Token is namespaced per username in localStorage so concurrent sessions
 * in different tabs don't overwrite each other.
 */
import { create } from 'zustand';

export interface AuthUser {
  id: string;
  username: string;
  email: string;
  is_admin: boolean;
}

interface AuthState {
  token: string | null;
  user: AuthUser | null;
  isAuthenticated: boolean;
  login: (token: string, user: AuthUser) => void;
  logout: () => void;
}

const STORAGE_PREFIX = 'surgicalai-auth-';
const LEGACY_KEY = 'surgicalai-auth';

/** Find any existing session in localStorage (namespaced or legacy). */
function loadSession(): { token: string; user: AuthUser } | null {
  // Check namespaced keys first
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key?.startsWith(STORAGE_PREFIX)) {
      try {
        const data = JSON.parse(localStorage.getItem(key) || '');
        if (data.token && data.user) return data;
      } catch { /* skip corrupt entries */ }
    }
  }
  // Migrate from legacy Zustand persist key
  try {
    const raw = localStorage.getItem(LEGACY_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      const state = parsed?.state || parsed;
      if (state?.token && state?.user) {
        // Migrate to namespaced key
        const u = state.user;
        localStorage.setItem(
          `${STORAGE_PREFIX}${u.username}`,
          JSON.stringify({ token: state.token, user: u })
        );
        localStorage.removeItem(LEGACY_KEY);
        return { token: state.token, user: u };
      }
    }
  } catch { /* skip */ }
  return null;
}

const initial = loadSession();

export const useAuthStore = create<AuthState>()((set, get) => ({
  token: initial?.token || null,
  user: initial?.user || null,
  isAuthenticated: !!initial?.token,

  login: (token, user) => {
    // Clear legacy key if present
    localStorage.removeItem(LEGACY_KEY);
    // Store with user-namespaced key
    localStorage.setItem(
      `${STORAGE_PREFIX}${user.username}`,
      JSON.stringify({ token, user })
    );
    // Move the URL off /login so the app lives at "/" (URL discipline:
    // "/" = app when authed, homepage when not; /login = login form only).
    try {
      if (window.location.pathname === '/login') {
        window.history.replaceState(null, '', '/');
      }
    } catch { /* ignore - auth state change below still renders the app */ }
    set({ token, user, isAuthenticated: true });
  },

  logout: () => {
    const user = get().user;
    if (user) {
      localStorage.removeItem(`${STORAGE_PREFIX}${user.username}`);
    }
    localStorage.removeItem(LEGACY_KEY);
    // Land on the marketing homepage, never the login form (modern UX:
    // logout -> homepage, which offers "Login" in its nav).
    try {
      window.history.replaceState(null, '', '/');
    } catch { /* ignore - state change below still renders LandingPage */ }
    set({ token: null, user: null, isAuthenticated: false });
  },
}));
