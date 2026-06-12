# Element Picker — Integration Guide

All picker logic lives in **new files**. Three existing files need minimal edits:

---

## New files (7 total — zero existing code modified)

| File | Purpose |
|------|---------|
| `stores/elementPickerStore.ts` | Zustand store — pickMode, selections, formattedContext |
| `components/ElementPicker/pickerScript.ts` | Vanilla JS injected into iframe — hover/click/deselect/ESC |
| `components/ElementPicker/previewUtils.ts` | Import-stub utilities (mirrors LivePreview helpers) |
| `components/ElementPicker/PickablePreview.tsx` | Self-contained Sandpack preview with picker injected |
| `components/ElementPicker/ElementPickerToolbar.tsx` | Bottom bar — 🎯 toggle, element chips, Copy, Clear |
| `components/ElementPicker/UploadPreview.tsx` | Watches sessionFiles for visual uploads → shows instant preview + picker |
| `components/ElementPicker/index.ts` | Barrel exports |

---

## Existing file changes (3 files, ~15 lines total)

### 1. `ChatPanel.tsx` — add UploadPreview + inject element context (~10 lines)

**Import (top of file):**
```tsx
import { UploadPreview } from './ElementPicker'
import { useElementPickerStore } from '../stores/elementPickerStore'
```

**Render UploadPreview (between compacting banner and messages div, ~line 1393):**
```tsx
      {/* Compacting banner */}
      {isCompacting && ( ... )}

      {/* Upload preview with element picker */}
      <UploadPreview />

      {/* Messages */}
      <div className="flex-1 overflow-y-auto">
```

**Inject element context into prompt (inside handleSend, before doStream call):**
```tsx
    // Prepend element picker context if any elements are selected
    const elementContext = useElementPickerStore.getState().formattedContext()
    const finalText = elementContext ? `${elementContext}\n\n${text}` : text

    doStream(sessionId, finalText, isFirstMessage, autoNameSession)
```

### 2. `InlineDiffCard.tsx` — swap preview in pick mode (~5 lines)

**Import (top of file):**
```tsx
import { PickablePreview, ElementPickerToolbar } from './ElementPicker'
import { useElementPickerStore } from '../stores/elementPickerStore'
```

**Inside the render, where LivePreview is shown (around line 835):**
```tsx
      {isVisualFile(filename) && showFilePreview && (
        <div className="mt-3 rounded-xl overflow-hidden border border-border/30">
          {useElementPickerStore.getState().pickMode ? (
            <>
              <PickablePreview
                code={modifiedCode ?? originalCode}
                filename={filename}
                sessionId={sessionId}
                fileId={fileData.file_id}
                pickMode={true}
              />
              <ElementPickerToolbar />
            </>
          ) : (
            <LivePreview ... />   {/* existing code unchanged */}
          )}
        </div>
      )}
```

---

## Flow

1. **Upload path:** User uploads .tsx/.jsx/.html → `UploadPreview` auto-detects → shows preview bar → user clicks to expand → picks elements → types prompt → element context prepended
2. **Post-AI path:** AI responds with visual code → InlineDiffCard shows LivePreview → user can toggle pick mode → picks elements → types follow-up → element context prepended
3. **No visual files:** Everything hidden — zero UI impact
