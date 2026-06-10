/**
 * File size validation for SurgicalAI uploads.
 *
 * Limits:
 *   Text/code files: 1 MB per file
 *   Image files:    10 MB per file
 *   Session total:  20 MB across all files
 */

export const MAX_TEXT_FILE_BYTES  = 1  * 1024 * 1024   // 1 MB
export const MAX_IMAGE_FILE_BYTES = 10 * 1024 * 1024   // 10 MB
export const MAX_SESSION_BYTES    = 20 * 1024 * 1024   // 20 MB

const IMAGE_EXTENSIONS = new Set([
  'png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp', 'svg', 'heic', 'heif',
])

export function getFileLimit(filename: string): number {
  const ext = filename.split('.').pop()?.toLowerCase() || ''
  return IMAGE_EXTENSIONS.has(ext) ? MAX_IMAGE_FILE_BYTES : MAX_TEXT_FILE_BYTES
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
    const kind = IMAGE_EXTENSIONS.has(ext) ? 'Image' : 'Text/code'
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
