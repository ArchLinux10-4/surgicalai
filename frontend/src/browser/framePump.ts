/** Drop every binary frame that arrives while one is still decoding, keeping the newest. */

export interface FrameGate {
  decoding: boolean
  pending: ArrayBuffer | null
}

export function createFrameGate(): FrameGate {
  return { decoding: false, pending: null }
}

/** Returns the buffer to decode now, or null when this frame only replaces the pending one. */
export function takeBinaryFrame(gate: FrameGate, buf: ArrayBuffer): ArrayBuffer | null {
  if (gate.decoding) {
    gate.pending = buf
    return null
  }
  gate.decoding = true
  return buf
}

/** After a decode finishes, the newest frame that arrived during it, or null. */
export function nextBinaryFrame(gate: FrameGate): ArrayBuffer | null {
  const pending = gate.pending
  gate.pending = null
  if (!pending) {
    gate.decoding = false
    return null
  }
  return pending
}

let paint: ((bmp: ImageBitmap) => void) | null = null
let clearPaint: (() => void) | null = null

export function setFramePainter(
  fn: ((bmp: ImageBitmap) => void) | null,
  clear?: (() => void) | null,
) {
  paint = fn
  clearPaint = clear || null
}

export function paintFrame(bmp: ImageBitmap) {
  if (paint) paint(bmp)
  else bmp.close()
}

export function clearPaintedFrame() {
  clearPaint?.()
}
