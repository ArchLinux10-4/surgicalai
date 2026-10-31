import { useCallback, useEffect, useState } from 'react'
import { browserApi, missingBrowser } from './browserClient'
import { applyStatus, useBrowserSocket } from './useBrowserSocket'
import { useBrowserStore } from './browserStore'
import { AddressBar } from './AddressBar'
import { TabStrip } from './TabStrip'
import { BrowserViewport } from './BrowserViewport'
import { ConsoleDrawer } from './ConsoleDrawer'
import { StyleInspector } from './StyleInspector'
import { PickHandoff } from './PickHandoff'
import { browserStrings } from './strings'

export function BrowserTab() {
  const [box, setBox] = useState<{ w: number; h: number; dpr: number } | null>(null)
  const { send } = useBrowserSocket(true, box)
  const onBox = useCallback((next: { w: number; h: number; dpr: number }) => {
    setBox((prev) => (prev && prev.w === next.w && prev.h === next.h && prev.dpr === next.dpr ? prev : next))
  }, [])
  const lastError = useBrowserStore((s) => s.lastError)
  const starting = useBrowserStore((s) => s.starting)

  useEffect(() => {
    let cancelled = false
    useBrowserStore.setState({ starting: true, lastError: null, navError: null })
    browserApi.start()
      .then((status) => {
        if (cancelled) return
        applyStatus(status)
        useBrowserStore.setState({ starting: false, sessionUsed: true })
      })
      .catch((err: any) => {
        if (cancelled) return
        useBrowserStore.setState({ starting: false, lastError: err?.message || browserStrings.missingTitle })
      })
    return () => { cancelled = true }
  }, [])

  const navigate = (url: string) => {
    useBrowserStore.setState({ navError: null, loading: true })
    browserApi.navigate(url).then(applyStatus).catch((err: any) => {
      useBrowserStore.setState({ loading: false, navError: err?.message || browserStrings.navErrorTitle })
    })
  }

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <TabStrip />
      <AddressBar onNavigate={navigate} />
      {missingBrowser(lastError) && !starting ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center">
          <p className="text-sm font-semibold text-ink">{browserStrings.missingTitle}</p>
          <p className="max-w-md text-[12px] text-muted">{browserStrings.missingBody}</p>
        </div>
      ) : (
        <div className="flex min-h-0 flex-1">
          <div className="flex min-w-0 flex-1 flex-col">
            <BrowserViewport box={box} onBox={onBox} send={send} onNavigate={navigate} onReload={() => browserApi.reload().then(applyStatus).catch(() => {})} />
            <ConsoleDrawer />
          </div>
          <StyleInspector />
        </div>
      )}
      <PickHandoff />
    </div>
  )
}
