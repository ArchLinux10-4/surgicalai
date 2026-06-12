/**
 * Utility functions for PickablePreview.
 * Mirrors the minimal set of helpers from LivePreview needed to set up
 * a Sandpack workspace — kept separate so LivePreview is never touched.
 */

/* ── Detect the primary exported component name ────────────────── */
export function detectComponent(code: string): string {
  return (
    code.match(/export\s+default\s+(?:function|class)\s+([A-Z]\w*)/)?.[1] ||
    code.match(/export\s+default\s+([A-Z]\w*)/)?.[1] ||
    code.match(/export\s+(?:function|class)\s+([A-Z][a-z]\w*)/)?.[1] ||
    [...code.matchAll(/(?:function|const)\s+([A-Z][a-z][a-zA-Z0-9]*)\s*[=(]/g)].pop()?.[1] ||
    'App'
  )
}

/* ── Stub builder — creates a safe proxy for unresolvable imports ── */
function buildStub(spec: string, pathComment: string): string {
  const names = spec
    .replace(/\*\s+as\s+(\w+)/, '$1')
    .replace(/[{}]/g, '')
    .split(',')
    .map((s: string) => s.trim().split(/\s+as\s+/).pop()?.trim())
    .filter((n): n is string => !!n && /^\w+$/.test(n))

  if (!names.length) return `// [no names to stub from: ${pathComment}]`

  return names.map((n) =>
    `const ${n}: any = (() => { ` +
    `const DATA=/^(user|loading|isLoading|error|data|status|count|list|items|token|id|name|email|value|config|options|state|type|mode|size|length|theme|session|message|result|response|success|ready|open|visible|active|enabled|disabled|selected|checked|collapsed|expanded|setupRequired|isAuthenticated|isAdmin|isDark|isLight|isOpen|isMobile|isDesktop)$/;` +
    `const mk=(t)=>new Proxy(function(){},{` +
    `get:(_,k)=>{if(typeof k==='symbol')return undefined;if(k==='__esModule')return undefined;` +
    `if(k==='then'){if(!t)return undefined;return function(r){try{r&&r({data:{}});}catch(e){}return mk(true);};}` +
    `if(DATA.test(String(k)))return undefined;return mk(true);},` +
    `apply:()=>mk(true),construct:()=>({})});return mk(false); })(); ` +
    `// stubbed from: ${pathComment}`
  ).join('\n')
}

/* ── Strip / stub unresolvable imports so Sandpack doesn't crash ── */
export function prepareCode(raw: string): string {
  let code = raw
  code = code.replace(
    /^import\s+type\s+.+?from\s+['"][^'"]+['"]\s*;?\s*$/gm,
    '// [type import removed for preview]'
  )
  code = code.replace(
    /^import\s+['"]([^'"]+)['"]\s*;?\s*$/gm,
    (_, path) => `// [side-effect import removed: ${path}]`
  )
  code = code.replace(
    /^import\s+(.+?)\s+from\s+['"](@\/[^'"]+)['"]\s*;?\s*$/gm,
    (_, spec, path) => buildStub(spec, `@/ alias: ${path}`)
  )
  code = code.replace(
    /^import\s+(.+?)\s+from\s+['"](\\.{1,2}[^'"]+)['"]\s*;?\s*$/gm,
    (_, spec, path) => buildStub(spec, path)
  )
  if (/import\.meta\.env/.test(code)) {
    const envStub = `const __import_meta_env__ = typeof import.meta !== 'undefined' && import.meta.env ? import.meta.env : {};\n`
    code = envStub + code.replace(/import\.meta\.env/g, '__import_meta_env__')
  }
  return code
}

/* ── Sandpack base CSS reset ───────────────────────────────────── */
export const BASE_INDEX_CSS = [
  '*, *::before, *::after { box-sizing: border-box; }',
  'html, body { margin: 0; padding: 0; width: 100%; height: 100%; background: transparent; }',
  '#root { width: 100%; height: 100%; }',
].join(' ')

/* ── Sandpack base dependencies ────────────────────────────────── */
export const BASE_DEPS: Record<string, string> = {
  'lucide-react': 'latest',
  'class-variance-authority': 'latest',
  'clsx': 'latest',
  'tailwind-merge': 'latest',
}
