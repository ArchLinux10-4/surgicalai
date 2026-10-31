/* Injected into every document of the local in-app browser.
   Hover draws a box from getBoundingClientRect. Click serializes the node
   and hands it to the Playwright bindings __surgicalHover / __surgicalPick. */
(() => {
  if (window.__surgicalPickInstalled) return
  window.__surgicalPickInstalled = true

  const COMPUTED_KEYS = [
    'display', 'position', 'width', 'height', 'margin', 'padding', 'border',
    'border-radius', 'color', 'background-color', 'background-image',
    'font-family', 'font-size', 'font-weight', 'line-height', 'letter-spacing',
    'text-align', 'opacity', 'box-shadow', 'gap', 'flex-direction',
    'justify-content', 'align-items', 'grid-template-columns',
  ]

  let armed = false
  let overlay = null
  let label = null
  let hoverQueued = false
  let lastEvent = null

  function cssEscape(value) {
    if (window.CSS && typeof CSS.escape === 'function') return CSS.escape(value)
    return String(value).replace(/[^a-zA-Z0-9_-]/g, '\\$&')
  }

  function stableClass(name) {
    if (!name) return false
    if (/_[A-Za-z0-9]{5,}$/.test(name)) return false
    if (/-\d/.test(name)) return false
    return true
  }

  function ensureOverlay() {
    if (overlay && overlay.isConnected) return
    overlay = document.createElement('div')
    overlay.id = '__surgical_overlay'
    overlay.style.cssText = [
      'position:fixed', 'pointer-events:none', 'z-index:2147483647',
      'border:2px solid #5b8cff', 'background:rgba(91,140,255,.12)',
      'border-radius:3px', 'transition:all 40ms linear', 'display:none',
      'box-sizing:border-box',
    ].join(';')
    label = document.createElement('div')
    label.style.cssText = [
      'position:absolute', 'left:0', 'top:-18px', 'background:#1e2433',
      'color:#d6e2ff', 'font:11px/16px ui-monospace,monospace',
      'padding:0 6px', 'border-radius:4px', 'white-space:nowrap',
      'pointer-events:none', 'max-width:280px', 'overflow:hidden',
    ].join(';')
    overlay.appendChild(label)
    ;(document.documentElement || document.body).appendChild(overlay)
  }

  function labelFor(el) {
    const tag = el.tagName.toLowerCase()
    const id = el.id ? '#' + el.id : ''
    const cls = Array.from(el.classList || []).filter(stableClass).slice(0, 2)
    return '<' + tag + '>' + id + (cls.length ? '.' + cls.join('.') : '')
  }

  function cssPath(el) {
    const parts = []
    let node = el
    while (node && node.nodeType === 1 && node.tagName !== 'HTML') {
      if (node === document.body) {
        parts.unshift('body')
        break
      }
      if (node.id) {
        const idSel = '#' + cssEscape(node.id)
        try {
          if (document.querySelectorAll(idSel).length === 1) {
            parts.unshift(idSel)
            break
          }
        } catch (e) { /* keep walking */ }
      }
      let seg = node.tagName.toLowerCase()
      const classes = Array.from(node.classList || []).filter(stableClass).slice(0, 2)
      if (classes.length) seg += classes.map((c) => '.' + cssEscape(c)).join('')
      const parent = node.parentElement
      if (parent) {
        const same = Array.from(parent.children).filter((c) => c.tagName === node.tagName)
        if (same.length > 1) seg += ':nth-of-type(' + (same.indexOf(node) + 1) + ')'
      }
      parts.unshift(seg)
      const path = parts.join(' > ')
      try {
        if (document.querySelectorAll(path).length === 1) return path
      } catch (e) { /* incomplete path */ }
      node = node.parentElement
    }
    return parts.join(' > ')
  }

  function xpath(el) {
    if (el.id) return '//*[@id="' + el.id.replace(/"/g, '') + '"]'
    const parts = []
    let node = el
    while (node && node.nodeType === 1) {
      let ix = 1
      let sib = node.previousElementSibling
      while (sib) {
        if (sib.tagName === node.tagName) ix += 1
        sib = sib.previousElementSibling
      }
      parts.unshift(node.tagName.toLowerCase() + '[' + ix + ']')
      node = node.parentElement
    }
    return '/' + parts.join('/')
  }

  function reactInfo(el) {
    const out = { component: '', file: '', line: 0, owners: [] }
    const marked = el.closest && el.closest('[data-component]')
    if (marked && marked.dataset && marked.dataset.component) out.component = marked.dataset.component
    const vue = el.__vueParentComponent
    if (vue && vue.type) {
      if (vue.type.__file) out.file = vue.type.__file
      out.component = out.component || vue.type.name || vue.type.displayName || ''
    }
    const key = Object.keys(el).find((k) => k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$'))
    if (!key) return (out.component || out.file) ? out : null
    let fiber = el[key]
    const owners = []
    let found = null
    for (let guard = 0; fiber && guard < 25; guard += 1) {
      const type = fiber.type
      if (type && (typeof type === 'function' || typeof type === 'object')) {
        const name = type.displayName || type.name || ''
        if (name && owners.length < 3 && owners.indexOf(name) === -1) owners.push(name)
        const src = fiber._debugSource
        if (!found && name) {
          found = {
            component: name,
            file: (src && src.fileName) || '',
            line: (src && src.lineNumber) || 0,
          }
        }
      }
      fiber = fiber.return
    }
    if (!found && !out.component && !out.file) return null
    return {
      component: (found && found.component) || out.component,
      file: (found && found.file) || out.file,
      line: (found && found.line) || 0,
      owners: owners.slice(0, 3),
    }
  }

  function outerHtml(el) {
    let html = el.outerHTML || ''
    html = html.replace(/<svg\b[\s\S]*?<\/svg>/gi, '<svg>…</svg>')
    const endTag = '</' + 'script>'
    html = html.replace(new RegExp('<script\\b[\\s\\S]*?' + endTag, 'gi'), '<script>…' + endTag)
    if (html.length > 4000) html = html.slice(0, 4000) + '…'
    return html
  }

  function serialize(el, token) {
    const rect = el.getBoundingClientRect()
    const cs = getComputedStyle(el)
    const computed = {}
    COMPUTED_KEYS.forEach((key) => { computed[key] = cs.getPropertyValue(key) })
    const attributes = {}
    Array.from(el.attributes || []).forEach((attr) => {
      if (attr.name === 'style' || attr.name === 'data-surgical-pick') return
      attributes[attr.name] = String(attr.value || '').slice(0, 200)
    })
    return {
      tag: el.tagName.toLowerCase(),
      id: el.id || null,
      classes: Array.from(el.classList || []),
      attributes,
      text: (el.innerText || '').trim().slice(0, 500),
      outerHTML: outerHtml(el),
      cssPath: cssPath(el),
      xpath: xpath(el),
      rect: {
        x: rect.x, y: rect.y, width: rect.width, height: rect.height,
        scrollX: window.scrollX, scrollY: window.scrollY,
      },
      computed,
      react: reactInfo(el),
      pageUrl: location.href,
      token,
    }
  }

  function placeOverlay(el) {
    ensureOverlay()
    const rect = el.getBoundingClientRect()
    overlay.style.display = 'block'
    overlay.style.left = rect.left + 'px'
    overlay.style.top = rect.top + 'px'
    overlay.style.width = Math.max(0, rect.width) + 'px'
    overlay.style.height = Math.max(0, rect.height) + 'px'
    label.textContent = labelFor(el)
    label.style.top = rect.top < 22 ? (rect.height + 2) + 'px' : '-18px'
  }

  function emitHover() {
    hoverQueued = false
    if (!armed || !lastEvent) return
    const el = document.elementFromPoint(lastEvent.clientX, lastEvent.clientY)
    if (!el || el === overlay || (overlay && overlay.contains(el))) return
    placeOverlay(el)
    if (typeof window.__surgicalHover === 'function') {
      const rect = el.getBoundingClientRect()
      window.__surgicalHover({
        rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
        tag: el.tagName.toLowerCase(),
        cursor: getComputedStyle(el).cursor || 'auto',
      })
    }
  }

  function onMove(ev) {
    if (!armed) return
    lastEvent = ev
    if (hoverQueued) return
    hoverQueued = true
    requestAnimationFrame(emitHover)
  }

  function onClick(ev) {
    if (!armed) return
    ev.preventDefault()
    ev.stopImmediatePropagation()
    const el = document.elementFromPoint(ev.clientX, ev.clientY)
    if (!el || el === overlay || (overlay && overlay.contains(el))) return
    const token = Math.random().toString(36).slice(2, 10)
    el.dataset.surgicalPick = token
    const payload = serialize(el, token)
    payload.shiftKey = !!ev.shiftKey
    placeOverlay(el)
    overlay.style.borderColor = '#7dffb3'
    setTimeout(() => { if (overlay) overlay.style.borderColor = '#5b8cff' }, 180)
    if (typeof window.__surgicalPick === 'function') window.__surgicalPick(payload)
    if (!ev.shiftKey) window.__surgicalSetPick(false)
  }

  function onKey(ev) {
    if (!armed) return
    if (ev.key === 'Escape') {
      ev.preventDefault()
      ev.stopPropagation()
      window.__surgicalSetPick(false)
    }
  }

  window.__surgicalSetPick = function (on) {
    const next = !!on
    if (next === armed) {
      if (!next && overlay) overlay.style.display = 'none'
      return
    }
    armed = next
    ensureOverlay()
    if (armed) {
      document.addEventListener('mousemove', onMove, true)
      document.addEventListener('click', onClick, true)
      document.addEventListener('keydown', onKey, true)
      document.documentElement.style.cursor = 'crosshair'
    } else {
      document.removeEventListener('mousemove', onMove, true)
      document.removeEventListener('click', onClick, true)
      document.removeEventListener('keydown', onKey, true)
      document.documentElement.style.cursor = ''
      overlay.style.display = 'none'
    }
  }
})()
