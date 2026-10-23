import { useEffect, useRef, useState } from 'react'
import { useAppStore } from '../stores/appStore'
import { AwsLevelPill } from './AwsApprovalCard'

function AwsOutput({ stdout, stderr, files }: { stdout: string; stderr: string; files: string }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLPreElement>(null)
  const [overflow, setOverflow] = useState(false)
  const signature = `${stdout}\n${stderr}\n${files}`
  useEffect(() => {
    const el = ref.current
    if (!el || open) return
    setOverflow(el.scrollHeight > el.clientHeight + 2)
  }, [signature, open])
  if (!stdout && !stderr && !files) return null
  return (
    <>
      <pre
        ref={ref}
        className={`whitespace-pre-wrap mt-1 ${open ? '' : 'max-h-40 overflow-y-auto'}`}
      >
        {stdout}
        {stderr && <span className="text-danger">{stdout ? '\n' : ''}{stderr}</span>}
        {files && <span>{(stdout || stderr) ? '\n' : ''}{files}</span>}
      </pre>
      {!open && overflow && (
        <button
          type="button"
          className="mt-1 text-[11px] font-semibold text-accent"
          onClick={() => setOpen(true)}
        >
          Show all
        </button>
      )}
    </>
  )
}

export function AwsCliFeed() {
  const activeSessions = useAppStore(s => s.activeSessions)
  const lines = useAppStore(s => s.awsTerminalLines)
  const visible = lines
    .map((line, idx) => ({ line, idx }))
    .filter(item => activeSessions && item.line.sessionId === activeSessions)
    .slice(-3)
  if (!visible.length) return null
  return (
    <div className="mx-3 mb-2 space-y-1">
      {visible.map(({ line, idx }) => {
        const files = (line.files || [])
          .map(f => `${f.name}:\n${f.content}`)
          .join('\n')
        return (
          <div key={idx} className="rounded-lg border border-border bg-[#100808] px-3 py-2 font-mono text-[11px] text-[#d6d6d6]">
            <div className="flex items-start gap-2">
              <AwsLevelPill level={line.level} />
              <div className="text-accent break-all">$ {line.command}</div>
            </div>
            <AwsOutput stdout={line.stdout || ''} stderr={line.stderr || ''} files={files} />
          </div>
        )
      })}
    </div>
  )
}
