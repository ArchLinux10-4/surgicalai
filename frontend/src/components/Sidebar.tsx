import React, { useEffect, useState, useRef } from 'react'
import { useAppStore } from '../stores/appStore'
import { useAuthStore } from '../stores/authStore'
import { api } from '../api/client'
import { FileNode } from '../types'
import { toast } from '../lib/toast'
import { ContextPanel } from './ContextPanel'
import { GitHubPanel } from './GitHubPanel'
import { LinearPanel } from './LinearPanel'
import { useThemeStore } from '../stores/themeStore'
import { Add, Bolt, Chat, Close, Code, DarkMode, Delete, Description, Edit, FileUpload, GitHub, KeyboardArrowDown, KeyboardArrowLeft, KeyboardArrowRight, LightMode, Logout, PushPin, Search, Settings } from '@mui/icons-material';

// ── File icon helper ─────────────────────────────────────────────────────────
const FILE_ICONS: Record<string, string> = {
  '.py': '🐍', '.js': '🟨', '.ts': '🔷', '.tsx': '⚛️', '.jsx': '⚛️',
  '.go': '🐹', '.rs': '🦀', '.java': '☕', '.cs': '💜', '.cpp': '🔵', '.c': '🔵',
  '.html': '🌐', '.css': '🎨', '.scss': '🎨', '.json': '📋', '.md': '📝',
  '.sh': '⚡', '.bash': '⚡', '.sql': '🗄️', '.yaml': '⚙️', '.yml': '⚙️',
  '.toml': '⚙️', '.env': '🔑', '.gitignore': '🙈', '.lock': '🔒',
}
function fileIcon(node: FileNode) {
  if (node.type === 'dir') return null
  return FILE_ICONS[node.extension || ''] || '📄'
}

// ── File Tree Node ────────────────────────────────────────────────────────────
function FileTreeNode({ node, depth = 0 }: { node: FileNode; depth?: number }) {
  const [expanded, setExpanded] = useState(depth < 1)
  const { setActiveFile, setRightTab } = useAppStore()
  const isDir = node.type === 'dir'
  const indent = depth * 14

  const handleClick = async () => {
    if (isDir) { setExpanded(!expanded); return }
    try {
      const file = await api.files.read(node.path)
      setActiveFile(file)
      setRightTab('editor')
    } catch (e: any) {
      toast.error('Cannot open file', e.message)
    }
  }

  return (
    <div>
      <div
        onClick={handleClick}
        className="flex items-center gap-1.5 py-[3px] pr-2 rounded-md text-[13px] cursor-pointer select-none group transition-colors hover:bg-overlay"
        style={{ paddingLeft: `${8 + indent}px` }}
      >
        {isDir ? (
          <span className="text-faint w-3 flex-shrink-0">
            {expanded ? <KeyboardArrowDown sx={{ fontSize: 11 }} /> : <KeyboardArrowRight sx={{ fontSize: 11 }} />}
          </span>
        ) : (
          <span className="w-3 flex-shrink-0" />
        )}
        <span className="text-[11px] flex-shrink-0">{isDir ? (expanded ? '📂' : '📁') : fileIcon(node)}</span>
        <span className={`truncate ${isDir ? 'text-accent font-medium' : 'text-ink'}`}>{node.name}</span>
        {node.size && node.size > 50000 && (
          <span className="ml-auto text-[10px] text-faint flex-shrink-0">{Math.round(node.size / 1024)}k</span>
        )}
      </div>
      {isDir && expanded && node.children?.map((child) => (
        <FileTreeNode key={child.path} node={child} depth={depth + 1} />
      ))}
    </div>
  )
}

// ── Timestamp helper ──────────────────────────────────────────────────────────
function relativeTime(iso: string): string {
  if (!iso) return ''
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  if (days < 7) return days === 1 ? 'yesterday' : `${days}d ago`
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

// ── Session Item ──────────────────────────────────────────────────────────────
function SessionItem({ session, active, onLoad, onDelete, onRename }: {
  session: any; active: boolean
  onLoad: () => void; onDelete: () => void; onRename: (title: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(session.title)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { if (editing) inputRef.current?.focus() }, [editing])

  const commitRename = () => {
    if (draft.trim() && draft !== session.title) onRename(draft.trim())
    setEditing(false)
  }

  return (
    <div
      className={`group relative flex items-start gap-2.5 px-3 py-2.5 cursor-pointer border-l-2 transition-all ${
        active
          ? 'bg-overlay border-accent text-ink'
          : 'border-transparent hover:bg-overlay/60 text-muted hover:text-ink'
      }`}
      onClick={() => !editing && onLoad()}
    >
      <Chat sx={{ fontSize: 13 }} className={`flex-shrink-0 mt-0.5 ${active ? 'text-accent' : 'text-faint'}`} />
      <div className="flex-1 min-w-0">
        {editing ? (
          <input
            ref={inputRef}
            value={draft}
            onChange={e => setDraft(e.target.value)}
            onBlur={commitRename}
            onKeyDown={e => { if (e.key === 'Enter') commitRename(); if (e.key === 'Escape') setEditing(false) }}
            className="w-full bg-transparent border-b border-accent outline-none text-[13px] text-ink"
            onClick={e => e.stopPropagation()}
          />
        ) : (
          <p className="text-[13px] leading-snug truncate">{session.title}</p>
        )}
        <p className="text-[11px] text-faint mt-0.5">{relativeTime(session.updated_at || session.created_at)}</p>
      </div>
      {!editing && (
        <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity flex-shrink-0">
          <button onClick={e => { e.stopPropagation(); setEditing(true) }}
            className="p-0.5 rounded hover:bg-overlay text-faint hover:text-ink transition-colors">
            <Edit sx={{ fontSize: 11 }} />
          </button>
          <button onClick={e => { e.stopPropagation(); onDelete() }}
            className="p-0.5 rounded hover:bg-overlay text-faint hover:text-red-400 transition-colors">
            <Delete sx={{ fontSize: 11 }} />
          </button>
        </div>
      )}
    </div>
  )
}

// ── Rail items config ─────────────────────────────────────────────────────────
type TabId = 'sessions' | 'files' | 'github' | 'context' | 'linear'

// Linear icon (inline SVG)
function LinearIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" fill="currentColor">
      <path d="M1.22541 61.5228c-.2225-.9485.90748-1.5459 1.59638-.857L39.3342 97.1782c.6889.6889.0915 1.8189-.857 1.5964C20.0515 94.2409 5.75949 79.9489 1.22541 61.5228zM.00189135 46.8891c-.01764375.2833.08887225.5599.28957985.7606L52.3503 99.7085c.2007.2007.4773.3072.7606.2896 2.3692-.1476 4.6938-.46 6.9624-.9259.7645-.157 1.0301-1.0963.4782-1.6481L2.57595 39.4485c-.55186-.5519-1.49117-.2863-1.648174.4782-.465666 2.2686-.77832 4.5932-.92588 6.9624zM4.21111 32.217c-.16499.3086-.10307.6839.15059.9278L66.8827 95.7893c.2439.2537.6192.3156.9278.1506 1.8364-.9832 3.6014-2.0639 5.2888-3.2349.5151-.3631.5636-1.0957.1024-1.5569L9.97262 30.8852c-.46113-.4612-1.19376-.4127-1.55688.1024-1.17101 1.6874-2.25171 3.4524-3.23463 5.2294zM12.6587 22.2145c-.3762-.3762-.3762-.9864 0-1.3625C23.1581 10.3514 37.4823 3.48085 53.3921 2.67806c.7845-.04036 1.4361.59114 1.4321 1.37753l-.1701 34.69671c-.0027.5533-.3462 1.0452-.8675 1.2518l-1.7984.7013c-.4897.191-1.0527.0861-1.4368-.2981L12.6587 22.2145zM29.8038 86.8099 13.1901 70.1962c-.5145-.5145-.3991-1.3745.2381-1.7235l18.8163-10.5469c.5501-.3082 1.2311-.1673 1.624.3368l13.2584 17.0328c.3929.5041.3241 1.2137-.1594 1.6327l-15.5549 13.4605c-.4998.4325-1.2729.3819-1.7088-.1287z"/>
    </svg>
  )
}

const RAIL_ITEMS: { id: TabId; icon: any; label: string; tooltip: string }[] = [
  { id: 'sessions', icon: Chat,        label: 'Chats',  tooltip: 'Chats' },
  { id: 'files',    icon: Code,        label: 'Files',  tooltip: 'Session Files' },
  { id: 'github',   icon: GitHub,      label: 'GitHub', tooltip: 'GitHub' },
  { id: 'linear',   icon: LinearIcon,  label: 'Linear', tooltip: 'Linear Issues' },
  { id: 'context',  icon: PushPin,     label: 'Pinned', tooltip: 'Pinned & Memory' },
]
