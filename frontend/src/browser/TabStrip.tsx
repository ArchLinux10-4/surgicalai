import { Add, Close } from '@mui/icons-material'
import { browserApi, type TabInfo } from './browserClient'
import { applyStatus } from './useBrowserSocket'
import { useBrowserStore } from './browserStore'

function label(tab: TabInfo): string {
  if (tab.title) return tab.title
  if (!tab.display_url) return 'New tab'
  try {
    return new URL(tab.display_url).host || tab.display_url
  } catch {
    return tab.display_url
  }
}

export function TabStrip() {
  const tabs = useBrowserStore((s) => s.tabs)
  const shown = tabs.length ? tabs : [{ index: 0, title: '', display_url: '', loading: false, favicon: '', active: true }]

  const activate = (index: number) => {
    if (shown[index]?.active) return
    browserApi.activateTab(index).then(applyStatus).catch(() => {})
  }

  const close = (index: number) => {
    browserApi.closeTab(index).then(applyStatus).catch(() => {})
  }

  const add = () => {
    browserApi.newTab().then((status) => {
      applyStatus(status)
      useBrowserStore.getState().requestAddressFocus()
    }).catch(() => {})
  }

  return (
    <div className="flex items-end gap-1 border-b border-border bg-base/60 px-2 pt-1">
      {shown.map((tab) => (
        <div
          key={tab.index}
          onMouseDown={(event) => {
            if (event.button === 1 && shown.length > 1) {
              event.preventDefault()
              close(tab.index)
            }
          }}
          className={`group flex max-w-[220px] items-center gap-1 rounded-t-md px-2 py-1 text-[11px] ${
            tab.active ? 'bg-overlay text-ink' : 'text-muted hover:bg-overlay/60 hover:text-ink'
          }`}
        >
          <button type="button" onClick={() => activate(tab.index)} className="flex min-w-0 flex-1 items-center gap-1.5" title={tab.display_url || label(tab)}>
            {tab.favicon ? (
              <img src={tab.favicon} alt="" className="h-3 w-3 shrink-0" />
            ) : (
              <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${tab.loading ? 'bg-accent' : 'bg-muted/50'}`} />
            )}
            <span className="truncate">{label(tab)}</span>
          </button>
          {shown.length > 1 && (
            <button
              type="button"
              title="Close tab"
              onClick={() => close(tab.index)}
              className="shrink-0 rounded p-0.5 text-muted hover:bg-overlay hover:text-ink"
            >
              <Close sx={{ fontSize: 12 }} />
            </button>
          )}
        </div>
      ))}
      <button type="button" title="New tab" onClick={add} className="mb-0.5 rounded-md p-1 text-muted hover:bg-overlay hover:text-ink">
        <Add sx={{ fontSize: 16 }} />
      </button>
    </div>
  )
}
