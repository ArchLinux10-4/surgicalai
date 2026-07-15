/**
 * File size validation for SurgicalAI uploads.
 *
 * Limits:
 *   Text/code files: 1 MB per file
 *   Image files:    10 MB per file
 *   PDF files:      15 MB per file
 *   Session total:  30 MB across all files
 */

export const MAX_TEXT_FILE_BYTES  = 1  * 1024 * 1024   // 1 MB
export const MAX_IMAGE_FILE_BYTES = 10 * 1024 * 1024   // 10 MB
export const MAX_PDF_FILE_BYTES   = 15 * 1024 * 1024   // 15 MB
export const MAX_SESSION_BYTES    = 30 * 1024 * 1024   // 30 MB

const IMAGE_EXTENSIONS = new Set([
  'png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp', 'svg', 'heic', 'heif',
])

const PDF_EXTENSIONS = new Set(['pdf'])

export function getFileLimit(filename: string): number {
  const ext = filename.split('.').pop()?.toLowerCase() || ''
  if (IMAGE_EXTENSIONS.has(ext)) return MAX_IMAGE_FILE_BYTES
  if (PDF_EXTENSIONS.has(ext)) return MAX_PDF_FILE_BYTES
  return MAX_TEXT_FILE_BYTES
}

/**
 * Returns an error message if the file is too large, or null if OK.
 */
export function validateFileSize(filename: string, sizeBytes: number): string | null {
  const limit = getFileLimit(filename)
  if (sizeBytes > limit) {
    const limitMB = (limit / (1024 * 1024)).toFixed(0)
    const sizeMB  = (sizeBytes / (1024 * 1024)).toFixed(1)
    const ext = filename.split('.').pop()?.toLowerCase() || ''
    let kind = 'Text/code'
    if (IMAGE_EXTENSIONS.has(ext)) kind = 'Image'
    else if (PDF_EXTENSIONS.has(ext)) kind = 'PDF'
    return `${kind} file too large: ${sizeMB}MB (limit: ${limitMB}MB)`
  }
  return null
}

/**
 * Returns an error message if adding this file would exceed the session limit, or null if OK.
 */
export function validateSessionTotal(currentTotal: number, newBytes: number): string | null {
  if (currentTotal + newBytes > MAX_SESSION_BYTES) {
    const totalMB = (MAX_SESSION_BYTES / (1024 * 1024)).toFixed(0)
    return `Session file limit exceeded (${totalMB}MB max)`
  }
  return null
}
