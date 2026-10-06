import { useAppStore } from '../stores/appStore'

export function AwsCliFeed() {
  const activeSessions = useAppStore(s => s.activeSessions)
  const lines = useAppStore(s => s.awsTerminalLines)
  const visible = lines.filter(l => activeSessions && l.sessionId === activeSessions).slice(-3)
  if (!visible.length) return null
  return (
    <div className="mx-3 mb-2 space-y-1">
      {visible.map((line, i) => (
        <div key={i} className="rounded-lg border border-border bg-[#100808] px-3 py-2 font-mono text-[11px] text-[#d6d6d6]">
          <div className="text-accent">$ {line.command}</div>
          {line.stdout && <pre className="whitespace-pre-wrap mt-1">{line.stdout}</pre>}
          {line.stderr && <pre className="whitespace-pre-wrap mt-1 text-danger">{line.stderr}</pre>}
        </div>
      ))}
    </div>
  )
}
