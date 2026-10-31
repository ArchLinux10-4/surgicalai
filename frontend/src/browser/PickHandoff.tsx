import { browserApi } from './browserClient'
import { useBrowserStore } from './browserStore'
import { browserStrings } from './strings'
import { useAppStore } from '../stores/appStore'

export function PickHandoff() {
  const node = useBrowserStore((s) => s.lastPick)
  if (!node) return null
  const label = `<${node.tag}>${node.el_id ? '#' + node.el_id : ''}`
  const shot = node.screenshot_png_b64 ? `data:image/png;base64,${node.screenshot_png_b64}` : ''
  return (
    <div className="pointer-events-none absolute bottom-4 left-1/2 z-20 -translate-x-1/2">
      <div className="pointer-events-auto flex items-center gap-2 rounded-xl border border-accent/30 bg-surface px-2 py-1.5 shadow-lg">
        {shot && <img src={shot} alt="" className="h-8 w-12 rounded object-cover" />}
        <div className="min-w-0">
          <div className="font-mono text-[11px] font-semibold text-accent">{label}</div>
          <div className="text-[10px] text-muted">{browserStrings.added}</div>
        </div>
        <button
          type="button"
          className="rounded-md bg-accent px-2 py-1 text-[11px] font-semibold text-white"
          onClick={() => useAppStore.getState().setBrowserOpen(false)}
        >
          {browserStrings.askInChat}
        </button>
        <button
          type="button"
          className="rounded-md border border-border px-2 py-1 text-[11px] text-ink"
          onClick={() => {
            useBrowserStore.setState({ lastPick: null })
            browserApi.pickMode(true).catch(() => {})
          }}
        >
          {browserStrings.pickAnother}
        </button>
      </div>
    </div>
  )
}
