/**
 * Self-contained picker script that runs INSIDE the preview iframe.
 * Communicates with the parent via postMessage.
 *
 * Messages IN  (parent → iframe):
 *   sai-picker-enable   — enter pick mode
 *   sai-picker-disable  — leave pick mode
 *   sai-picker-clear    — clear all selections
 *
 * Messages OUT (iframe → parent):
 *   sai-picker-ready       — script initialized
 *   sai-element-selected   — element picked  { index, data }
 *   sai-element-deselected — element unpicked { index }
 *   sai-picker-escaped     — user pressed ESC
 */

export const PICKER_SCRIPT = `
(function() {
  var pickMode = false;
  var hoveredEl = null;
  var selected = [];
  var nextIdx = 1;

  /* ── Init ───────────────────────────────────────────── */
  function init() {
    if (!document.body) { requestAnimationFrame(init); return; }
    document.addEventListener('mousemove', onHover, true);
    document.addEventListener('click', onClick, true);
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('message', onMessage);
    window.parent.postMessage({ type: 'sai-picker-ready' }, '*');
  }

  /* ── Parent commands ────────────────────────────────── */
  function onMessage(e) {
    if (!e.data) return;
    if (e.data.type === 'sai-picker-enable')  { pickMode = true; document.body.style.cursor = 'crosshair'; }
    if (e.data.type === 'sai-picker-disable') { pickMode = false; document.body.style.cursor = ''; clearHover(); }
    if (e.data.type === 'sai-picker-clear')   { clearAll(); }
  }

  /* ── CSS selector builder ───────────────────────────── */
  function getSelector(el) {
    var parts = [];
    var node = el;
    while (node && node !== document.body && node !== document.documentElement) {
      var tag = node.tagName.toLowerCase();
      if (node.id) { parts.unshift(tag + '#' + node.id); break; }
      var cls = typeof node.className === 'string' ? node.className.trim() : '';
      if (cls) tag += '.' + cls.split(/\\\\s+/).slice(0, 2).join('.');
      var parent = node.parentElement;
      if (parent) {
        var same = Array.from(parent.children).filter(function(s) { return s.tagName === node.tagName; });
        if (same.length > 1) tag += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
      }
      parts.unshift(tag);
      node = node.parentElement;
    }
    return parts.join(' > ');
  }

  /* ── Truncated outer HTML ───────────────────────────── */
  function getHtml(el) {
    var clone = el.cloneNode(false);
    if (el.children.length > 0) clone.innerHTML = '<!-- ' + el.children.length + ' child elements -->';
    else {
      var t = (el.textContent || '').slice(0, 120);
      clone.textContent = t;
    }
    var h = clone.outerHTML;
    return h.length > 500 ? h.slice(0, 500) + '...' : h;
  }

  /* ── Find in selected array ─────────────────────────── */
  function findIdx(el) {
    for (var i = 0; i < selected.length; i++) { if (selected[i].el === el) return i; }
    return -1;
  }

  /* ── Hover ──────────────────────────────────────────── */
  function onHover(e) {
    if (!pickMode) return;
    var el = e.target;
    if (el === hoveredEl) return;
    clearHover();
    if (el === document.body || el === document.documentElement) return;
    hoveredEl = el;
    if (findIdx(el) === -1) {
      el.style.outline = '2px solid #3b82f6';
      el.style.outlineOffset = '2px';
    }
  }

  function clearHover() {
    if (hoveredEl && findIdx(hoveredEl) === -1) {
      hoveredEl.style.outline = '';
      hoveredEl.style.outlineOffset = '';
    }
    hoveredEl = null;
  }

  /* ── Click — toggle select / deselect ───────────────── */
  function onClick(e) {
    if (!pickMode) return;
    e.preventDefault();
    e.stopPropagation();
    var el = e.target;
    if (el === document.body || el === document.documentElement) return;

    var si = findIdx(el);
    if (si !== -1) {
      /* Deselect */
      el.style.outline = '';
      el.style.outlineOffset = '';
      var entry = selected.splice(si, 1)[0];
      window.parent.postMessage({ type: 'sai-element-deselected', index: entry.idx }, '*');
    } else {
      /* Select */
      var num = nextIdx++;
      el.style.outline = '2px solid #10b981';
      el.style.outlineOffset = '2px';
      selected.push({ el: el, idx: num });

      var rect = el.getBoundingClientRect();
      var cs = window.getComputedStyle(el);
      window.parent.postMessage({
        type: 'sai-element-selected',
        index: num,
        data: {
          tag: el.tagName.toLowerCase(),
          id: el.id || null,
          classes: (typeof el.className === 'string' && el.className.trim())
            ? el.className.trim().split(/\\\\s+/) : [],
          selector: getSelector(el),
          text: (el.textContent || '').trim().slice(0, 120),
          html: getHtml(el),
          styles: {
            color: cs.color,
            backgroundColor: cs.backgroundColor,
            fontSize: cs.fontSize,
            fontWeight: cs.fontWeight,
            padding: cs.padding,
            margin: cs.margin,
            display: cs.display,
            borderRadius: cs.borderRadius
          }
        }
      }, '*');
    }
  }

  /* ── ESC exits pick mode ────────────────────────────── */
  function onKey(e) {
    if (e.key === 'Escape' && pickMode) {
      pickMode = false;
      document.body.style.cursor = '';
      clearHover();
      window.parent.postMessage({ type: 'sai-picker-escaped' }, '*');
    }
  }

  /* ── Clear all selections ───────────────────────────── */
  function clearAll() {
    selected.forEach(function(entry) {
      entry.el.style.outline = '';
      entry.el.style.outlineOffset = '';
    });
    selected = [];
    nextIdx = 1;
  }

  /* ── Start ──────────────────────────────────────────── */
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function() { setTimeout(init, 50); });
  } else {
    setTimeout(init, 50);
  }
})();
`;
