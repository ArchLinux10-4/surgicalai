/**
 * LoginPage — handles two states:
 *   1. setup_required = true  → first-run "Create Admin Account" form
 *   2. setup_required = false → standard login form
 */
import { useState, useEffect, FormEvent } from 'react';
import { useAuthStore } from '../stores/authStore';
import { apiClient } from '../api/client';

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
      <div className="min-h-screen bg-gray-950 flex items-center justify-center">
        <div className="w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-950 flex items-center justify-center p-4">
      <div className="w-full max-w-md">
        {/* Logo */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-14 h-14 rounded-2xl bg-indigo-600 mb-4">
            <svg className="w-8 h-8 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M17.25 6.75L22.5 12l-5.25 5.25m-10.5 0L1.5 12l5.25-5.25m7.5-3l-4.5 16.5" />
            </svg>
          </div>
          <h1 className="text-2xl font-bold text-white">SurgicalAI</h1>
          <p className="text-gray-400 text-sm mt-1">
            {setupRequired ? 'Create your admin account to get started' : 'Sign in to your account'}
          </p>
        </div>

        {/* Card */}
        <div className="bg-gray-900 border border-gray-800 rounded-2xl p-8 shadow-2xl">
          {setupRequired && (
            <div className="mb-6 p-3 rounded-lg bg-indigo-500/10 border border-indigo-500/30">
              <p className="text-indigo-300 text-sm font-medium">First time setup</p>
              <p className="text-indigo-400/70 text-xs mt-0.5">This account will have admin access to manage users.</p>
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-300 mb-1.5">Username</label>
              <input
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="your-username"
                required
                minLength={3}
                className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3.5 py-2.5 text-white placeholder-gray-500 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent transition"
              />
            </div>

            {setupRequired && (
              <div>
                <label className="block text-sm font-medium text-gray-300 mb-1.5">Email <span className="text-gray-500 font-normal">(optional)</span></label>
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3.5 py-2.5 text-white placeholder-gray-500 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent transition"
                />
              </div>
            )}

            <div>
              <label className="block text-sm font-medium text-gray-300 mb-1.5">Password</label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={setupRequired ? 'Min. 8 characters' : '••••••••'}
                required
                minLength={setupRequired ? 8 : 1}
                className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3.5 py-2.5 text-white placeholder-gray-500 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent transition"
              />
            </div>

            {error && (
              <div className="p-3 rounded-lg bg-red-500/10 border border-red-500/30">
                <p className="text-red-400 text-sm">{error}</p>
              </div>
            )}

            <button
              type="submit"
              disabled={loading}
              className="w-full bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed text-white font-medium rounded-lg px-4 py-2.5 text-sm transition flex items-center justify-center gap-2"
            >
              {loading ? (
                <>
                  <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                  {setupRequired ? 'Creating account…' : 'Signing in…'}
                </>
              ) : (
                setupRequired ? 'Create Admin Account' : 'Sign In'
              )}
            </button>
          </form>
        </div>

        <p className="text-center text-gray-600 text-xs mt-6">
          SurgicalAI · Precision Code Editor
        </p>
      </div>
    </div>
  );
}
