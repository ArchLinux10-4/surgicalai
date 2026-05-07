/**
 * AdminUsersPanel — manage users (admin only).
 * Shown inside the Settings modal on the Users tab.
 */
import React, { useState, useEffect } from 'react'
import { useAuthStore } from '../stores/authStore'
import { api } from '../api/client'
import { Plus, Trash2, Shield, User, RefreshCw } from 'lucide-react'

interface UserRecord {
  id: string
  username: string
  email: string | null
  is_admin: number | boolean
  is_active: number | boolean
  created_at: string
  last_login: string | null
}

interface CreateForm {
  username: string
  email: string
  password: string
  is_admin: boolean
}



export function AdminUsersPanel() {
  const { user } = useAuthStore()
  const [users, setUsers] = useState<UserRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState<CreateForm>({ username: '', email: '', password: '', is_admin: false })
  const [formError, setFormError] = useState('')
  const [deletingId, setDeletingId] = useState<string | null>(null)

  async function loadUsers() {
    setLoading(true)
    try {
      const data = await api.auth.getUsers()
      setUsers(data)
    } catch (e: any) {
      console.error('Failed to load users:', e.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadUsers() }, [])

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setFormError('')
    setCreating(true)
    try {
      await api.auth.createUser(form)
      setForm({ username: '', email: '', password: '', is_admin: false })
      setShowForm(false)
      await loadUsers()
    } catch (e: any) {
      setFormError(e.message)
    } finally {
      setCreating(false)
    }
  }

  async function handleDelete(userId: string, username: string) {
    if (!confirm(`Delete user "${username}"? This cannot be undone.`)) return
    setDeletingId(userId)
    try {
      await api.auth.deleteUser(userId)
      await loadUsers()
    } catch (e: any) {
      alert(e.message)
    } finally {
      setDeletingId(null)
    }
  }

  if (!user?.is_admin) {
    return (
      <div className="flex items-center justify-center h-32 text-faint text-sm">
        Admin access required.
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* Header row */}
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-ink">User Management</h3>
          <p className="text-xs text-faint mt-0.5">{users.length} user{users.length !== 1 ? 's' : ''} total</p>
        </div>
        <div className="flex gap-2">
          <button onClick={loadUsers} className="btn-icon" title="Refresh">
            <RefreshCw size={13} className={loading ? 'animate-spin' : ''} />
          </button>
          <button
            onClick={() => setShowForm(!showForm)}
            className="flex items-center gap-1.5 text-xs bg-accent text-white px-3 py-1.5 rounded-lg hover:bg-accent/80 transition font-medium"
          >
            <Plus size={13} />
            New User
          </button>
        </div>
      </div>

      {/* Create user form */}
      {showForm && (
        <form onSubmit={handleCreate} className="bg-overlay border border-border rounded-xl p-4 space-y-3">
          <p className="text-xs font-semibold text-ink">Create New User</p>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-xs text-faint mb-1 block">Username *</label>
              <input
                type="text"
                required
                minLength={3}
                value={form.username}
                onChange={(e) => setForm({ ...form, username: e.target.value })}
                placeholder="username"
                className="w-full bg-surface border border-border rounded-lg px-2.5 py-1.5 text-xs text-ink placeholder-faint focus:outline-none focus:ring-1 focus:ring-accent"
              />
            </div>
            <div>
              <label className="text-xs text-faint mb-1 block">Email</label>
              <input
                type="email"
                value={form.email}
                onChange={(e) => setForm({ ...form, email: e.target.value })}
                placeholder="user@example.com"
                className="w-full bg-surface border border-border rounded-lg px-2.5 py-1.5 text-xs text-ink placeholder-faint focus:outline-none focus:ring-1 focus:ring-accent"
              />
            </div>
          </div>
          <div>
            <label className="text-xs text-faint mb-1 block">Password * (min. 8 chars)</label>
            <input
              type="password"
              required
              minLength={8}
              value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
              placeholder="••••••••"
              className="w-full bg-surface border border-border rounded-lg px-2.5 py-1.5 text-xs text-ink placeholder-faint focus:outline-none focus:ring-1 focus:ring-accent"
            />
          </div>
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={form.is_admin}
              onChange={(e) => setForm({ ...form, is_admin: e.target.checked })}
              className="rounded"
            />
            <span className="text-xs text-ink">Grant admin access</span>
          </label>
          {formError && (
            <p className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-lg px-2.5 py-1.5">
              {formError}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => { setShowForm(false); setFormError('') }}
              className="text-xs text-faint hover:text-ink px-3 py-1.5 rounded-lg transition"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={creating}
              className="flex items-center gap-1.5 text-xs bg-accent text-white px-3 py-1.5 rounded-lg hover:bg-accent/80 disabled:opacity-50 transition font-medium"
            >
              {creating ? <RefreshCw size={11} className="animate-spin" /> : <Plus size={11} />}
              Create User
            </button>
          </div>
        </form>
      )}

      {/* Users list */}
      <div className="space-y-1.5">
        {loading ? (
          <div className="flex items-center justify-center h-16">
            <RefreshCw size={14} className="animate-spin text-faint" />
          </div>
        ) : users.length === 0 ? (
          <p className="text-xs text-faint text-center py-4">No users found.</p>
        ) : (
          users.map((u) => (
            <div
              key={u.id}
              className="flex items-center justify-between px-3 py-2.5 rounded-xl bg-overlay border border-border hover:border-accent/30 transition"
            >
              <div className="flex items-center gap-2.5">
                <div className="w-7 h-7 rounded-full bg-indigo-600/20 flex items-center justify-center text-indigo-400 text-[11px] font-bold">
                  {u.username.slice(0, 2).toUpperCase()}
                </div>
                <div>
                  <div className="flex items-center gap-1.5">
                    <span className="text-xs font-medium text-ink">{u.username}</span>
                    {Boolean(u.is_admin) && (
                      <span className="flex items-center gap-0.5 text-[10px] px-1.5 py-0.5 rounded-full bg-indigo-500/15 text-indigo-400 font-medium">
                        <Shield size={9} />
                        Admin
                      </span>
                    )}
                    {u.id === user?.id && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-green-500/15 text-green-400 font-medium">You</span>
                    )}
                    {!Boolean(u.is_active) && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-muted/15 text-muted font-medium">Inactive</span>
                    )}
                  </div>
                  {u.email && <p className="text-[11px] text-faint">{u.email}</p>}
                </div>
              </div>
              <div className="flex items-center gap-1">
                <span className="text-[10px] text-faint mr-2">
                  {u.last_login ? `Last login ${new Date(u.last_login).toLocaleDateString()}` : 'Never logged in'}
                </span>
                {u.id !== user?.id && (
                  <button
                    onClick={() => handleDelete(u.id, u.username)}
                    disabled={deletingId === u.id}
                    className="btn-icon text-faint hover:text-red-400 transition"
                    title="Delete user"
                  >
                    {deletingId === u.id ? <RefreshCw size={13} className="animate-spin" /> : <Trash2 size={13} />}
                  </button>
                )}
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
