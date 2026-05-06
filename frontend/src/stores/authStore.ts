/**
 * Auth store — JWT token, current user, login/logout.
 * Token is persisted in localStorage so sessions survive page reloads.
 */
import { create } from 'zustand';
import { persist } from 'zustand/middleware';

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

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      user: null,
      isAuthenticated: false,

      login: (token, user) => {
        set({ token, user, isAuthenticated: true });
      },

      logout: () => {
        set({ token: null, user: null, isAuthenticated: false });
      },
    }),
    {
      name: 'surgicalai-auth',
      partialize: (state) => ({ token: state.token, user: state.user, isAuthenticated: state.isAuthenticated }),
    }
  )
);
