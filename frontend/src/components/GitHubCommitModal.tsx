import React, { useState, useMemo } from 'react'
import { X, Github, GitBranch, GitPullRequest, Check, Loader2, ExternalLink, AlertCircle } from 'lucide-react'
import { api } from '../api/client'
import { toast } from '../lib/toast'
import type { SessionFile } from '../types'

interface GitHubMeta {
  owner: string
  repo: string
  branch: string
  path: string
  sha: string
}

interface GitHubCommitModalProps {
  sessionFiles: SessionFile[]
  onClose: () => void
  onSuccess: () => void
}

export function GitHubCommitModal({ sessionFiles, onClose, onSuccess }: GitHubCommitModalProps) {
  // Only files with github_meta
  const ghFiles = useMemo(() => {
    return sessionFiles.filter(f => {
      if (!f.github_meta) return false
      try { JSON.parse(f.github_meta); return true } catch { return false }
    }).map(f => ({
      file: f,
      meta: JSON.parse(f.github_meta!) as GitHubMeta,
      isModified: !!(f.updated_at && f.updated_at !== f.created_at),
    }))
  }, [sessionFiles])

  const [selectedIds, setSelectedIds] = useState<Set<string>>(
    new Set(ghFiles.filter(f => f.isModified).map(f => f.file.id))
  )
  const [commitMsg, setCommitMsg] = useState('')
  const [createPR, setCreatePR] = useState(false)
  const [newBranch, setNewBranch] = useState('surgicalai/changes')
  const [prTitle, setPrTitle] = useState('Update files via SurgicalAI')
  const [committing, setCommitting] = useState(false)
  const [result, setResult] = useState<any>(null)

  // Determine repo/branch from first selected file
  const primaryMeta = ghFiles.find(f => selectedIds.has(f.file.id))?.meta

  const toggleFile = (id: string) => {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const handleCommit = async () => {
    if (!commitMsg.trim()) {
      toast.error('Commit message required')
      return
    }
    if (selectedIds.size === 0) {
      toast.error('Select at least one file')
      return
    }
    if (!primaryMeta) return

    setCommitting(true)
    try {
      const files = ghFiles
        .filter(f => selectedIds.has(f.file.id))
        .map(f => ({
          session_file_id: f.file.id,
          github_path: f.meta.path,
          sha: f.meta.sha,
        }))

      const body: any = {
        owner: primaryMeta.owner,
        repo: primaryMeta.repo,
        branch: primaryMeta.branch,
        message: commitMsg.trim(),
        files,
        create_pr: createPR,
      }
      if (createPR) {
        body.new_branch = newBranch.trim()
        body.pr_title = prTitle.trim()
      }

      const res: any = await (api as any).github.commit(body)
      setResult(res)
      toast.success(
        createPR ? `PR #${res.pr_number} opened` : `${res.committed.length} file(s) committed`,
        `Pushed to ${primaryMeta.owner}/${primaryMeta.repo}`
      )
      onSuccess()
    } catch (e: any) {
      toast.error('Commit failed', e.message)
    } finally {
      setCommitting(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-[200] flex items-center justify-center bg-black/70 backdrop-blur-sm"
      onClick={e => { if (e.target === e.currentTarget) onClose() }}
    >
      <div className="bg-surface border border-border rounded-2xl shadow-2xl shadow-black/40 w-full max-w-lg mx-4 overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-border">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-xl bg-gray-800 border border-border flex items-center justify-center">
              <Github size={15} className="text-ink" />
            </div>
            <div>
              <h2 className="text-[15px] font-bold text-ink">Push to GitHub</h2>
              {primaryMeta && (
                <p className="text-[11px] text-faint">
                  {primaryMeta.owner}/{primaryMeta.repo}
                  <span className="mx-1 opacity-40">·</span>
                  <GitBranch size={9} className="inline mr-0.5" />
                  {primaryMeta.branch}
                </p>
              )}
            </div>
          </div>
          <button onClick={onClose} className="btn-icon"><X size={14} /></button>
        </div>

        {/* Success state */}
        {result ? (
          <div className="px-5 py-6 flex flex-col items-center gap-4 text-center">
            <div className="w-14 h-14 rounded-full bg-emerald-500/15 border border-emerald-500/25 flex items-center justify-center">
              <Check size={24} className="text-emerald-400" />
            </div>
            <div>
              <p className="text-[15px] font-bold text-ink mb-1">
                {result.create_pr ? 'Pull Request Created!' : 'Changes Pushed!'}
              </p>
              <p className="text-[12px] text-faint">
                {result.committed?.length} file(s) committed to <strong>{result.branch}</strong>
              </p>
            </div>
            <div className="flex gap-2">
              {result.pr_url && (
                <a
                  href={result.pr_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-accent/10 text-accent text-[12px] font-semibold hover:bg-accent/20 border border-accent/20 transition-colors"
                >
                  <GitPullRequest size={12} />
                  View PR #{result.pr_number}
                  <ExternalLink size={10} />
                </a>
              )}
              {result.commit_url && !result.pr_url && (
                <a
                  href={result.commit_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-accent/10 text-accent text-[12px] font-semibold hover:bg-accent/20 border border-accent/20 transition-colors"
                >
                  View on GitHub
                  <ExternalLink size={10} />
                </a>
              )}
              <button onClick={onClose} className="px-3 py-1.5 rounded-lg text-[12px] font-medium text-muted border border-border hover:bg-overlay transition-colors">
                Close
              </button>
            </div>
          </div>
        ) : (
          <div className="px-5 py-4 flex flex-col gap-4">
            {/* File list */}
            <div>
              <p className="text-[11px] font-semibold text-muted uppercase tracking-wide mb-2">
                Files to commit ({selectedIds.size}/{ghFiles.length})
              </p>
              {ghFiles.length === 0 ? (
                <div className="flex items-center gap-2 px-3 py-3 rounded-lg bg-amber-500/5 border border-amber-500/20">
                  <AlertCircle size={13} className="text-amber-400 flex-shrink-0" />
                  <p className="text-[12px] text-amber-300">
                    No GitHub-sourced files in this session. Load files from a repo first.
                  </p>
                </div>
              ) : (
                <div className="space-y-1 max-h-40 overflow-y-auto">
                  {ghFiles.map(({ file, meta, isModified }) => (
                    <label
                      key={file.id}
                      className="flex items-center gap-3 px-3 py-2 rounded-lg hover:bg-overlay transition-colors cursor-pointer"
                    >
                      <input
                        type="checkbox"
                        checked={selectedIds.has(file.id)}
                        onChange={() => toggleFile(file.id)}
                        className="accent-accent w-3.5 h-3.5"
                      />
                      <div className="flex-1 min-w-0">
                        <p className="text-[12px] font-medium text-ink truncate">{file.filename}</p>
                        <p className="text-[10px] text-faint truncate">{meta.path}</p>
                      </div>
                      {isModified && (
                        <span className="flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 rounded-md flex-shrink-0">
                          <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 inline-block" />
                          AI-edited
                        </span>
                      )}
                    </label>
                  ))}
                </div>
              )}
            </div>

            {/* Commit message */}
            <div>
              <label className="block text-[11px] font-semibold text-muted uppercase tracking-wide mb-1.5">
                Commit message <span className="text-red-400">*</span>
              </label>
              <input
                type="text"
                value={commitMsg}
                onChange={e => setCommitMsg(e.target.value)}
                placeholder="Describe your changes…"
                className="w-full px-3 py-2 text-[13px] bg-surface border border-border rounded-lg text-ink placeholder:text-faint focus:outline-none focus:border-accent/60 transition-colors"
                onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) handleCommit() }}
              />
            </div>

            {/* Push mode */}
            <div>
              <p className="text-[11px] font-semibold text-muted uppercase tracking-wide mb-2">Push mode</p>
              <div className="space-y-2">
                <label className="flex items-start gap-3 cursor-pointer">
                  <input
                    type="radio"
                    name="pushMode"
                    checked={!createPR}
                    onChange={() => setCreatePR(false)}
                    className="mt-0.5 accent-accent"
                  />
                  <div>
                    <p className="text-[12px] font-medium text-ink">
                      Commit directly to <code className="text-accent text-[11px]">{primaryMeta?.branch || 'main'}</code>
                    </p>
                    <p className="text-[11px] text-faint">Changes go live immediately</p>
                  </div>
                </label>
                <label className="flex items-start gap-3 cursor-pointer">
                  <input
                    type="radio"
                    name="pushMode"
                    checked={createPR}
                    onChange={() => setCreatePR(true)}
                    className="mt-0.5 accent-accent"
                  />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5">
                      <p className="text-[12px] font-medium text-ink">Create new branch + Pull Request</p>
                      <GitPullRequest size={11} className="text-faint" />
                    </div>
                    <p className="text-[11px] text-faint">Safer — review before merging</p>
                  </div>
                </label>
              </div>
            </div>

            {/* PR options */}
            {createPR && (
              <div className="space-y-2 pl-6 border-l-2 border-accent/20">
                <div>
                  <label className="block text-[11px] text-muted mb-1">Branch name</label>
                  <input
                    type="text"
                    value={newBranch}
                    onChange={e => setNewBranch(e.target.value)}
                    className="w-full px-3 py-1.5 text-[12px] bg-surface border border-border rounded-lg text-ink focus:outline-none focus:border-accent/60"
                  />
                </div>
                <div>
                  <label className="block text-[11px] text-muted mb-1">PR title</label>
                  <input
                    type="text"
                    value={prTitle}
                    onChange={e => setPrTitle(e.target.value)}
                    className="w-full px-3 py-1.5 text-[12px] bg-surface border border-border rounded-lg text-ink focus:outline-none focus:border-accent/60"
                  />
                </div>
              </div>
            )}

            {/* Footer */}
            <div className="flex gap-2 pt-1">
              <button
                onClick={onClose}
                className="flex-1 px-4 py-2 rounded-lg text-[13px] font-medium text-muted border border-border hover:bg-overlay transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleCommit}
                disabled={committing || selectedIds.size === 0 || !commitMsg.trim() || ghFiles.length === 0}
                className="flex-1 flex items-center justify-center gap-2 px-4 py-2 rounded-lg text-[13px] font-semibold bg-gray-800 hover:bg-gray-700 text-white border border-gray-600 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {committing ? (
                  <><Loader2 size={13} className="animate-spin" /> Pushing…</>
                ) : (
                  <><Github size={13} /> {createPR ? 'Create PR' : 'Push to GitHub'}</>
                )}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
