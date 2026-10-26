import { api } from '../api/client'

export type ApplyDestination = 'session_copy' | 'disk'

export type ApplyFailure = {
  id: string
  code: string
  detail: string
}

const FAILURE_COPY: Record<string, string> = {
  already_applied: 'Already applied',
  anchor_not_found: 'The file changed since this edit was generated.',
  file_missing: 'The file is not in this session.',
  write_failed: 'The file could not be saved.',
}

export function applyFailureCopy(code: string | null | undefined): string {
  return FAILURE_COPY[code || ''] || 'The change could not be applied.'
}

export function notifyApplied() {
  window.dispatchEvent(new CustomEvent('sai-applied-refresh'))
}

export async function applyChanges(input: {
  sessionId?: string
  filename: string
  fileId?: string
  changes: any[]
  fileContent: string
  mode: 'one' | 'all'
  save?: boolean
  saveLabel?: string
}): Promise<{
  appliedIds: string[]
  failed: ApplyFailure[]
  destination: ApplyDestination
  modifiedContent: string
  raw: any
}> {
  const body = {
    file_path: input.filename,
    session_id: input.sessionId,
    change_id: input.changes[0]?.id,
    changes: input.changes,
    file_content: input.fileContent,
  }
  const raw = input.mode === 'one'
    ? await api.surgical.apply(body)
    : await api.surgical.applyAll(body)
  const failed: ApplyFailure[] = (Array.isArray(raw?.failed_changes) ? raw.failed_changes : []).map((item: any) => {
    const code = item?.code || (item?.already_applied ? 'already_applied' : 'anchor_not_found')
    return {
      id: String(item?.change_id || ''),
      code,
      detail: item?.detail || applyFailureCopy(code),
    }
  })
  const blocking = new Set(failed.filter(item => item.code !== 'already_applied').map(item => item.id))
  const appliedIds = input.changes
    .map(change => change?.id)
    .filter((id: string) => id && !blocking.has(id))
  let destination: ApplyDestination = 'session_copy'
  const modifiedContent = raw?.modified_content || ''
  if (input.save && input.sessionId && input.fileId && modifiedContent) {
    const saved = await api.sessionFiles.update(
      input.sessionId,
      input.fileId,
      modifiedContent,
      input.saveLabel,
    )
    if (saved?.written_to_disk) destination = 'disk'
  }
  return { appliedIds, failed, destination, modifiedContent, raw }
}
