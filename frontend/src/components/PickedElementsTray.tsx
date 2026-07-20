import { AdsClick, Close } from '@mui/icons-material'
import { useAppStore } from '../stores/appStore'

/** Compact chip row for elements picked via the Element Picker — docked
 *  above the composer, same visual language as the quick-prompt chip row
 *  right below it. Chips are additive context: removing one here never
 *  touches anything the user has typed in the textarea. */
export function PickedElementsTray() {
  const { pickedElements, removePickedElement } = useAppStore()
  if (pickedElements.length === 0) return null

  return (
    <div className="mb-2.5 flex flex-wrap gap-1.5">
      {pickedElements.map((el) => {
        const label = el.elId ? `#${el.elId}` : (el.className ? `.${el.className.split(' ')[0]}` : '')
        return (
          <div
            key={el.id}
            title={el.text ? `"${el.text.slice(0, 120)}"` : el.outerHTML.slice(0, 200)}
            className="flex items-center gap-1.5 pl-2 pr-1 py-1 rounded-lg bg-accent/10 border border-accent/25 text-[11px] text-accent font-medium max-w-[220px]"
          >
            <AdsClick sx={{ fontSize: 13 }} className="shrink-0" />
            <span className="font-mono truncate">&lt;{el.tag}&gt;{label}</span>
            <button
              onClick={() => removePickedElement(el.id)}
              className="shrink-0 p-0.5 rounded hover:bg-accent/20 transition-colors"
              title="Remove"
            >
              <Close sx={{ fontSize: 12 }} />
            </button>
          </div>
        )
      })}
    </div>
  )
}
