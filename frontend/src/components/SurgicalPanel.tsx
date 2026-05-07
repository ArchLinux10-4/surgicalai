import React, { useState } from 'react'
import { useAppStore } from '../stores/appStore'
import { api } from '../api/client'
import { SurgicalChange, ImpactAnalysis } from '../types'
import { CheckCircle, XCircle, AlertTriangle, ChevronDown, ChevronRight, Zap } from 'lucide-react'
import { toast } from '../lib/toast'
import { DiffView } from './DiffView'

const C = {
  bg: 'rgb(var(--c-base))', aiBg: 'rgb(var(--c-surface))', border: 'rgb(var(--c-border))',
  text: 'rgb(var(--c-ink))', muted: 'rgb(var(--c-muted))', accent: 'rgb(var(--c-accent))',
  success: 'rgb(var(--c-success))', warning: 'rgb(var(--c-warning))', danger: 'rgb(var(--c-danger))',
}

function ConfidenceBadge({ score }: { score: number }) {
  const color = score >= 8 ? C.success : score >= 6 ? C.warning : C.danger
  const label = score >= 8 ? 'High' : score >= 6 ? 'Medium' : 'Low'
  return (
    <span style={{ padding: '2px 8px', borderRadius: '20px', fontSize: '11px', fontWeight: 600, border: `1px solid ${color}`, color, background: `${color}20` }}>
      {label} {score}/10
    </span>
  )
}

function ImpactDisplay({ symbolPath, filePath, workspacePath }: { symbolPath: string; filePath: string; workspacePath: string }) {
  const [impact, setImpact] = useState<ImpactAnalysis | null>(null)
  const [loading, setLoading] = useState(false)

  const loadImpact = async () => {
    setLoading(true)
    try {
      const result = await api.context.getImpact(symbolPath, filePath, workspacePath)
      setImpact(result)
    } catch {}
    setLoading(false)
  }

  if (!impact && !loading) {
    return (
      <button
        onClick={loadImpact}
        style={{
          padding: '4px 10px', background: 'transparent', border: `1px solid ${C.border}`,
          borderRadius: '4px', color: C.muted, cursor: 'pointer', fontSize: '11px',
          display: 'flex', alignItems: 'center', gap: '4px'
        }}
      >
        🔍 Analyze Impact
      </button>
    )
  }

  if (loading) return <div style={{ fontSize: '11px', color: C.muted }}>Scanning...</div>
  if (!impact) return null

  const riskColor = impact.risk_level === 'high' ? C.danger : impact.risk_level === 'medium' ? C.warning : C.success

  return (
    <div style={{ marginTop: '8px', padding: '8px', background: C.bg, borderRadius: '6px', border: `1px solid ${riskColor}40` }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '6px' }}>
        <span style={{ fontSize: '11px', fontWeight: 700, color: riskColor, textTransform: 'uppercase' }}>
          {impact.risk_level} risk
        </span>
        <span style={{ fontSize: '11px', color: C.muted }}>{impact.summary}</span>
      </div>
      {impact.impacts.slice(0, 5).map((imp, i) => (
        <div key={i} style={{ fontSize: '11px', color: C.muted, padding: '2px 0', fontFamily: 'monospace' }}>
          → {imp.file_path.split('/').pop()} <span style={{ color: C.accent }}>({imp.impact_type})</span>
        </div>
      ))}
    </div>
  )
}

function ChangeCard({ change, onApply, onReject, applied }: {
  change: SurgicalChange
  onApply: (id: string) => void
  onReject: (id: string) => void
  applied: boolean
}) {
  const [expanded, setExpanded] = useState(false)
  const { activeFile, workspacePath } = useAppStore()

  return (
    <div style={{ border: `1px solid ${applied ? C.success : C.border}`, borderRadius: '8px', marginBottom: '12px', overflow: 'hidden', background: C.aiBg }}>
      {/* Card header */}
      <div
        onClick={() => setExpanded(!expanded)}
        style={{ padding: '12px', cursor: 'pointer', display: 'flex', alignItems: 'flex-start', gap: '10px' }}
      >
        {expanded ? <ChevronDown size={16} color={C.muted} style={{ marginTop: '2px', flexShrink: 0 }} /> : <ChevronRight size={16} color={C.muted} style={{ marginTop: '2px', flexShrink: 0 }} />}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px', flexWrap: 'wrap' }}>
            <span style={{ fontSize: '13px', fontWeight: 600, color: C.text, fontFamily: 'monospace' }}>
              {change.symbol.full_path || change.symbol.name}
            </span>
            <span style={{ fontSize: '11px', color: C.accent, background: 'rgb(var(--c-accent) / 0.12)', padding: '1px 6px', borderRadius: '4px' }}>
              {change.symbol.symbol_type}
            </span>
            <ConfidenceBadge score={change.confidence} />
            {applied && <span style={{ fontSize: '11px', color: C.success }}>✓ Applied</span>}
          </div>
          <div style={{ fontSize: '12px', color: C.muted }}>{change.description}</div>
          <div style={{ fontSize: '11px', color: C.muted, marginTop: '4px' }}>
            Lines {change.symbol.start_line}–{change.symbol.end_line}
          </div>
        </div>
        {!applied && (
          <div style={{ display: 'flex', gap: '6px', flexShrink: 0 }} onClick={e => e.stopPropagation()}>
            <button
              onClick={() => onReject(change.id)}
              style={{ background: 'none', border: `1px solid ${C.danger}`, color: C.danger, borderRadius: '4px', padding: '4px 10px', cursor: 'pointer', fontSize: '12px', fontWeight: 600 }}
            >
              ✕ Skip
            </button>
            <button
              onClick={() => onApply(change.id)}
              disabled={change.confidence < 5}
              style={{
                background: C.success, border: 'none', color: 'rgb(var(--c-base))',
                borderRadius: '4px', padding: '4px 10px', cursor: change.confidence < 5 ? 'not-allowed' : 'pointer',
                fontSize: '12px', fontWeight: 600, opacity: change.confidence < 5 ? 0.5 : 1
              }}
            >
              ✓ Apply
            </button>
          </div>
        )}
      </div>
      {/* Expanded diff */}
      {expanded && (
        <div style={{ borderTop: `1px solid ${C.border}` }}>
          <DiffView original={change.original_code} modified={change.new_code} />
          <div style={{ padding: '10px 12px', borderTop: `1px solid ${C.border}` }}>
            <ImpactDisplay
              symbolPath={change.symbol.full_path || change.symbol.name}
              filePath={activeFile?.path || ''}
              workspacePath={workspacePath || ''}
            />
          </div>
        </div>
      )}
    </div>
  )
}

export function SurgicalPanel() {
  const { surgicalAnalysis, setSurgicalAnalysis, activeFile, setActiveFile, addMessage, activeSessions } = useAppStore()
  const [appliedIds, setAppliedIds] = useState<Set<string>>(new Set())
  const [rejectedIds, setRejectedIds] = useState<Set<string>>(new Set())
  const [applying, setApplying] = useState<string | null>(null)

  if (!surgicalAnalysis) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100%', color: C.muted, padding: '40px' }}>
        <Zap size={32} color={C.border} style={{ marginBottom: '12px' }} />
        <div style={{ fontSize: '14px', fontWeight: 600, color: C.text, marginBottom: '8px' }}>No surgical analysis yet</div>
        <div style={{ fontSize: '13px', textAlign: 'center', lineHeight: '1.6' }}>
          Switch to <strong style={{ color: C.accent }}>✂️ Surgical</strong> mode in the chat,<br />
          open a file, and describe what to change.
        </div>
      </div>
    )
  }

  const { plan, changes } = surgicalAnalysis
  const pendingChanges = changes.filter(c => !appliedIds.has(c.id) && !rejectedIds.has(c.id))

  const handleApply = async (changeId: string) => {
    if (!activeFile) return
    setApplying(changeId)
    try {
      const result = await api.surgical.apply({
        file_path: activeFile.path,
        change_id: changeId,
        changes: changes,
        file_content: activeFile.content,
      })
      setActiveFile({ ...activeFile, content: result.new_content })
      setAppliedIds(prev => new Set([...prev, changeId]))
      // Cloud mode: file was uploaded (not on server disk) — offer download
      if (!result.backup_path) {
        const blob = new Blob([result.new_content], { type: 'text/plain' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = activeFile.path.split('/').pop() || 'modified_file'
        a.click()
        URL.revokeObjectURL(url)
        toast.success('Change applied — modified file downloaded!')
      }
      if (activeSessions) {
        addMessage({
          id: Date.now().toString(),
          session_id: activeSessions,
          role: 'assistant',
          content: result.backup_path
            ? `✅ Applied change to \`${changes.find(c => c.id === changeId)?.symbol.full_path}\`. Backup saved.`
            : `✅ Applied change to \`${changes.find(c => c.id === changeId)?.symbol.full_path}\`. Modified file downloaded (cloud mode).`,
          created_at: new Date().toISOString()
        })
      }
    } catch (e: any) {
      toast.error('Apply failed', e.message)
    }
    setApplying(null)
  }

  const handleApplyAll = async () => {
    if (!activeFile) return
    const toApply = changes.filter(c => !rejectedIds.has(c.id) && !appliedIds.has(c.id))
    if (toApply.length === 0) return
    setApplying('all')
    try {
      const result = await api.surgical.applyAll({
        file_path: activeFile.path,
        change_id: toApply[0].id,
        changes: toApply,
        file_content: activeFile.content,
      })
      // Cloud mode: file was uploaded (not on server disk) — offer download
      if (!result.backup_path) {
        const blob = new Blob([result.new_content], { type: 'text/plain' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = activeFile.path.split('/').pop() || 'modified_file'
        a.click()
        URL.revokeObjectURL(url)
        toast.success('All changes applied — modified file downloaded!')
      }
      setActiveFile({ ...activeFile, content: result.new_content })
      const newApplied = new Set(appliedIds)
      toApply.forEach(c => newApplied.add(c.id))
      setAppliedIds(newApplied)
    } catch (e: any) {
      toast.error('Apply all failed', e.message)
    }
    setApplying(null)
  }

  return (
    <div style={{ height: '100%', overflow: 'auto', padding: '16px', fontSize: '13px' }}>
      {/* Plan summary */}
      <div style={{ background: C.aiBg, border: `1px solid ${C.border}`, borderRadius: '8px', padding: '14px', marginBottom: '16px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
          <Zap size={14} color={C.accent} />
          <span style={{ fontSize: '13px', fontWeight: 700, color: C.text }}>Architect Plan</span>
          <span style={{ fontSize: '11px', color: C.muted, background: C.border, padding: '1px 6px', borderRadius: '4px' }}>
            {changes.length} changes
          </span>
        </div>
        <div style={{ fontSize: '13px', color: C.text, lineHeight: '1.6', marginBottom: '10px' }}>{plan.summary}</div>

        {plan.risks.length > 0 && (
          <div style={{ padding: '8px 10px', background: 'rgb(var(--c-warning) / 0.08)', border: '1px solid rgb(var(--c-warning) / 0.25)', borderRadius: '6px', marginBottom: '8px' }}>
            <div style={{ color: C.warning, fontSize: '11px', fontWeight: 600, marginBottom: '4px' }}>⚠️ Risks to review:</div>
            {plan.risks.map((r, i) => <div key={i} style={{ color: C.muted, fontSize: '12px' }}>• {r}</div>)}
          </div>
        )}

        {plan.import_changes.length > 0 && (
          <div style={{ padding: '8px 10px', background: 'rgb(var(--c-accent) / 0.06)', border: '1px solid rgb(var(--c-accent) / 0.19)', borderRadius: '6px', marginBottom: '8px' }}>
            <div style={{ color: C.accent, fontSize: '11px', fontWeight: 600, marginBottom: '4px' }}>Import changes needed:</div>
            {plan.import_changes.map((imp, i) => <div key={i} style={{ color: C.muted, fontSize: '12px', fontFamily: 'monospace' }}>{imp}</div>)}
          </div>
        )}

        {pendingChanges.length > 1 && (
          <button
            onClick={handleApplyAll}
            disabled={applying !== null}
            style={{
              marginTop: '8px', background: C.success, border: 'none', borderRadius: '6px',
              color: 'rgb(var(--c-base))', padding: '6px 16px', cursor: 'pointer', fontSize: '12px', fontWeight: 700,
              display: 'flex', alignItems: 'center', gap: '6px'
            }}
          >
            <Zap size={13} /> Apply All {pendingChanges.length} Changes
          </button>
        )}
      </div>

      {/* Changes */}
      {changes.map(change => (
        <ChangeCard
          key={change.id}
          change={change}
          applied={appliedIds.has(change.id)}
          onApply={handleApply}
          onReject={(id) => setRejectedIds(prev => new Set([...prev, id]))}
        />
      ))}

      {appliedIds.size === changes.length && changes.length > 0 && (
        <div style={{ textAlign: 'center', padding: '20px', color: C.success, fontSize: '14px', fontWeight: 600 }}>
          ✅ All changes applied! File saved.
        </div>
      )}
    </div>
  )
}
