export interface FrameParts {
  metadata: Record<string, unknown>
  jpeg: Uint8Array
}

/** Binary screencast frame: 4-byte big-endian metadata length, JSON, raw JPEG. */
export function parseFrame(buf: ArrayBuffer): FrameParts {
  const view = new DataView(buf)
  const length = view.getUint32(0, false)
  const metaBytes = new Uint8Array(buf, 4, length)
  const metadata = JSON.parse(new TextDecoder().decode(metaBytes)) as Record<string, unknown>
  const jpeg = new Uint8Array(buf, 4 + length)
  return { metadata, jpeg }
}
