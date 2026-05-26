/**
 * useCodeRain — matrix code rain canvas animation.
 * Matches the LoginPage CodeRain exactly: same chars, same green, same timing.
 * Returns a ref to attach to a <canvas> element.
 */
import { useEffect, useRef } from 'react'

const CODE_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789{}[]()<>=/\\|;:.,+-*&^%$#@!~`'

export function useCodeRain(active = true) {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    if (!active) return
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    let animFrameId: number
    let columns: number
    let drops: number[]
    const fontSize = 14
    const FRAME_INTERVAL = 27
    let lastTime = 0

    const resize = () => {
      canvas.width  = canvas.offsetWidth
      canvas.height = canvas.offsetHeight
      columns = Math.floor(canvas.width / fontSize)
      drops   = new Array(columns).fill(1)
    }
    resize()

    const draw = (timestamp: number) => {
      if (timestamp - lastTime < FRAME_INTERVAL) {
        animFrameId = requestAnimationFrame(draw)
        return
      }
      lastTime = timestamp
      ctx.fillStyle = 'rgba(0,0,0,0.06)'
      ctx.fillRect(0, 0, canvas.width, canvas.height)
      ctx.font = `${fontSize}px monospace`

      for (let i = 0; i < columns; i++) {
        const char       = CODE_CHARS[Math.floor(Math.random() * CODE_CHARS.length)]
        const brightness = Math.random()
        ctx.fillStyle    = brightness > 0.7
          ? '#4ade80'
          : `rgba(74,222,128,${0.15 + brightness * 0.3})`
        ctx.fillText(char, i * fontSize, drops[i] * fontSize)
        if (drops[i] * fontSize > canvas.height && Math.random() > 0.975) drops[i] = 0
        drops[i]++
      }
      animFrameId = requestAnimationFrame(draw)
    }

    animFrameId = requestAnimationFrame(draw)
    window.addEventListener('resize', resize)
    return () => {
      cancelAnimationFrame(animFrameId)
      window.removeEventListener('resize', resize)
    }
  }, [active])

  return canvasRef
}
