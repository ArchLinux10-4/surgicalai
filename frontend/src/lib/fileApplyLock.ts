// Cross-component mutual exclusion for file "apply" operations.
//
// Root cause this fixes (proven from a real Railway trace + code trace, not
// guessed): the global "Apply All" bar (ChatPanel.tsx: ApplyAllButton) and
// each individual diff card's own Apply button (InlineDiffCard.tsx:
// handleApplySelected) each independently do
//   read current content -> POST /api/surgical/apply(-all) -> write content back
// with ZERO coordination between them. If a user applies from a diff card
// while the global bar is mid-flight on the same file (or vice versa — e.g.
// two diff cards proposing changes to the same file in different messages),
// both read the same "before" content. Whichever call lands second is now
// operating on stale line numbers/content, so the deterministic engine's
// line-relocation falls through and the request 409s — this is exactly the
// "Stale lines N-M: content mismatch, relocation failed" + interleaved
// 409/200 pattern seen in the user-supplied Railway log at 23:09:19-23:09:29
// for the same files being edited from two apply paths at once.
//
// This module is a pure client-side mutex keyed by file_id. It does NOT
// change the apply algorithm, request shape, or success-path behavior in any
// way. When no other apply is in flight for a file (the normal, vast
// majority case), callers acquire the lock trivially and every existing,
// battle-tested code path runs completely unchanged. The lock only ever
// changes behavior in the rare case where two apply flows for the same file
// would otherwise race.
const locked = new Set<string>()
export const APPLY_LOCK_EVENT = 'sai-apply-lock-changed'

/** Try to acquire the apply lock for a file. Returns false if another apply
 *  is already in flight for this file_id — caller should not proceed. */
export function acquireApplyLock(fileId: string | undefined | null): boolean {
  if (!fileId) return true // no id to key on — never block, just let it run
  if (locked.has(fileId)) return false
  locked.add(fileId)
  window.dispatchEvent(new CustomEvent(APPLY_LOCK_EVENT, { detail: { fileId, locked: true } }))
  return true
}

/** Release the apply lock for a file. Always safe to call, even if the
 *  lock was never held (e.g. fileId was falsy). */
export function releaseApplyLock(fileId: string | undefined | null): void {
  if (!fileId) return
  locked.delete(fileId)
  window.dispatchEvent(new CustomEvent(APPLY_LOCK_EVENT, { detail: { fileId, locked: false } }))
}

export function isApplyLocked(fileId: string | undefined | null): boolean {
  return !!fileId && locked.has(fileId)
}
