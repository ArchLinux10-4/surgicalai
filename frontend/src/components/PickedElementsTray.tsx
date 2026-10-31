import { AdsClick, Close, Tune } from '@mui/icons-material'
import { api } from '../api/client'
import { useAppStore } from '../stores/appStore'
import { useBrowserStore } from '../browser/browserStore'

/** Compact chip row for elements and screenshots from the in-app browser. */
export function PickedElementsTray() {
  const {
    pickedElements, removePickedElement, browserShots, removeBrowserShot,
    activeSessions, removeSessionFile, settings, setBrowserOpen,
  } = useAppStore()
  if (pickedElements.length === 0 && browserShots.length === 0) return null
  const hosted = !!settings?.is_hosted

  const removeFile = (fileId?: string) => {
    if (fileId && activeSessions) {
      api.sessionFiles.delete(activeSessions, fileId).catch(() => {})
      removeSessionFile(fileId)
    }
  }

  const remove = (id: string, fileId?: string) => {
    removePickedElement(id)
    removeFile(fileId)
  }

  return (
    <div className="flex flex-wrap gap-1.5 px-3 pt-2.5">
      {pickedElements.map((el) => {
        const label = el.elId ? `#${el.elId}` : (el.className ? `.${el.className.split(' ')[0]}` : '')
        return (
          <div
            key={el.id}
            title={el.text ? `"${el.text.slice(0, 120)}"` : el.outerHTML.slice(0, 200)}
            className="flex items-center gap-1.5 pl-2 pr-1 py-1 rounded-lg bg-accent/10 border border-accent/25 text-[11px] text-accent font-medium max-w-[260px]"
          >
            {el.screenshot ? (
              <img src={el.screenshot} alt="" className="h-4 w-6 rounded object-cover shrink-0" />
            ) : (
              <AdsClick sx={{ fontSize: 13 }} className="shrink-0" />
            )}
            <span className="font-mono truncate">&lt;{el.tag}&gt;{label}</span>
            {!hosted && el.token && (
              <button
                onClick={() => {
                  useBrowserStore.setState({
                    inspectorOpen: true,
                    inspector: {
                      node: {
                        tag: el.tag,
                        el_id: el.elId,
                        classes: el.className ? el.className.split(' ') : [],
                        attributes: {},
                        text: el.text,
                        outer_html: el.outerHTML,
                        css_path: el.cssPath || '',
                        xpath: el.xpath || '',
                        rect: el.rect || { x: 0, y: 0, width: 0, height: 0 },
                        computed: el.computed || {},
                        react: el.react || null,
                        page_url: el.pageUrl || '',
                        token: el.token || '',
                      },
                      edits: el.styleEdits || {},
                      source: (el.source || []).map((hit) => ({
                        file: hit.file,
                        file_id: hit.file_id || '',
                        line: hit.line,
                        score: hit.score,
                        snippet: '',
                      })),
                    },
                  })
                  setBrowserOpen(true)
                }}
                className="shrink-0 p-0.5 rounded hover:bg-accent/20 transition-colors"
                title="Inspect"
              >
                <Tune sx={{ fontSize: 12 }} />
              </button>
            )}
            <button
              onClick={() => remove(el.id, el.screenshotFileId)}
              className="shrink-0 p-0.5 rounded hover:bg-accent/20 transition-colors"
              title="Remove"
            >
              <Close sx={{ fontSize: 12 }} />
            </button>
          </div>
        )
      })}
      {browserShots.map((shot) => (
        <div
          key={shot.id}
          title={`${shot.filename} · ${shot.pageUrl}`}
          className="flex items-center gap-1.5 pl-2 pr-1 py-1 rounded-lg bg-accent/10 border border-accent/25 text-[11px] text-accent font-medium max-w-[260px]"
        >
          <img src={shot.thumb} alt="" className="h-5 w-8 rounded object-cover shrink-0" />
          <span className="truncate">Screenshot</span>
          <button
            onClick={() => { removeBrowserShot(shot.id); removeFile(shot.fileId) }}
            className="shrink-0 p-0.5 rounded hover:bg-accent/20 transition-colors"
            title="Remove"
          >
            <Close sx={{ fontSize: 12 }} />
          </button>
        </div>
      ))}
    </div>
  )
}
