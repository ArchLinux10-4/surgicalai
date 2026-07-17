/**
 * tsc-as-a-service — Vercel serverless function (Option #2).
 *
 * Runs the TypeScript compiler IN-MEMORY (no spawned binary, no temp files)
 * and returns structured diagnostics in the EXACT shape the Python backend's
 * linter_validator already parses:
 *
 *     { "errors": [ {line, column, message, detail}, ... ], "tool": "tsc" }
 *
 * Why this exists: the backend runs on a Python host (Railway) where `tsc`
 * was never installed. Rather than bloat that container with a Node runtime,
 * we offload type-checking to Vercel, which is a native Node environment and
 * already ships `typescript` as a frontend dependency.
 *
 * ── SECURITY ────────────────────────────────────────────────────────────────
 * This endpoint type-checks ARBITRARY caller-supplied source. To stop it from
 * being a public abuse/DoS surface it requires a shared secret. Set the same
 * value in BOTH places:
 *   - Vercel env:  TSC_SERVICE_SECRET
 *   - Railway env: TSC_SERVICE_SECRET
 * The backend sends it as the `x-tsc-secret` header. If TSC_SERVICE_SECRET is
 * unset on Vercel the endpoint refuses all requests (fail-closed).
 *
 * ── ORDERING ────────────────────────────────────────────────────────────────
 * Do NOT enable the backend to call this (TSC_ENABLED=1) until PR #82's
 * DELTA lint gate is live. Checking a file in isolation emits many
 * "Cannot find module" errors; the delta gate cancels those out across
 * original-vs-edited. Without it this would false-block every large edit.
 */

import ts from 'typescript';
import type { VercelRequest, VercelResponse } from '@vercel/node';

interface TscError {
  line: number;
  column: number;
  message: string;
  detail: string;
  // `code` is the numeric TS diagnostic code prefixed with "TS" (e.g. "TS1005").
  // `kind` records which diagnostic pass produced it: only 'syntactic' errors
  // are a pure function of the single file and safe for the backend to BLOCK on
  // when type-checking in isolation (no node_modules / sibling modules). See the
  // backend _tsc_error_kind() rationale (session 6930f196 round 2).
  code: string;
  kind: 'syntactic' | 'semantic';
}

const MAX_SOURCE_BYTES = 2_000_000; // 2 MB guard
const MAX_ERRORS = 8; // mirror backend cap (errors[:8])

// Mirror backend _TSC_CONFIG_BASE compilerOptions.
const BASE_OPTIONS: ts.CompilerOptions = {
  target: ts.ScriptTarget.ES2020,
  module: ts.ModuleKind.ESNext,
  moduleResolution: ts.ModuleResolutionKind.Bundler,
  jsx: ts.JsxEmit.ReactJSX,
  strict: true,
  skipLibCheck: true,
  noEmit: true,
  isolatedModules: true,
  allowImportingTsExtensions: true,
};

function extOf(filename: string): string {
  const i = filename.lastIndexOf('.');
  return i >= 0 ? filename.slice(i).toLowerCase() : '';
}

function scriptKindFor(ext: string): ts.ScriptKind {
  switch (ext) {
    case '.tsx':
      return ts.ScriptKind.TSX;
    case '.jsx':
      return ts.ScriptKind.JSX;
    case '.js':
      return ts.ScriptKind.JS;
    default:
      return ts.ScriptKind.TS;
  }
}

/**
 * Type-check `code` in memory and return structured diagnostics.
 * Only diagnostics for OUR virtual source file are kept (lib.d.ts / node_modules
 * noise is excluded), matching the backend's _parse_tsc_output filtering.
 */
function checkSource(code: string, filename: string): TscError[] {
  const ext = extOf(filename);
  const allowJs = ext === '.js' || ext === '.jsx';
  const options: ts.CompilerOptions = allowJs
    ? { ...BASE_OPTIONS, allowJs: true, checkJs: true }
    : { ...BASE_OPTIONS };

  // Virtual filename inside a fresh "project root".
  const virtualName = `/__sa__/${filename.split('/').pop() || 'input' + (ext || '.ts')}`;
  const sourceFile = ts.createSourceFile(
    virtualName,
    code,
    options.target ?? ts.ScriptTarget.ES2020,
    /*setParentNodes*/ true,
    scriptKindFor(ext),
  );

  const defaultHost = ts.createCompilerHost(options, true);
  const host: ts.CompilerHost = {
    ...defaultHost,
    getSourceFile: (name, languageVersion, onError, shouldCreate) => {
      if (name === virtualName) return sourceFile;
      return defaultHost.getSourceFile(name, languageVersion, onError, shouldCreate);
    },
    fileExists: (name) => name === virtualName || defaultHost.fileExists(name),
    readFile: (name) => (name === virtualName ? code : defaultHost.readFile(name)),
    writeFile: () => {
      /* noEmit — swallow */
    },
  };

  const program = ts.createProgram([virtualName], options, host);
  // Tag each diagnostic with the pass that produced it. In isolation only
  // SYNTACTIC diagnostics are trustworthy (see TscError.kind rationale); the
  // backend blocks on introduced syntactic errors and treats semantic ones as
  // advisory. Syntactic diagnostics carry no TS code range that overlaps the
  // semantic set, but tagging by source is exact and future-proof.
  const tagged: Array<[ts.Diagnostic, 'syntactic' | 'semantic']> = [
    ...program.getSyntacticDiagnostics(sourceFile).map(
      (d) => [d, 'syntactic'] as [ts.Diagnostic, 'syntactic'],
    ),
    ...program.getSemanticDiagnostics(sourceFile).map(
      (d) => [d, 'semantic'] as [ts.Diagnostic, 'semantic'],
    ),
  ];

  const errors: TscError[] = [];
  for (const [d, kind] of tagged) {
    if (d.category !== ts.DiagnosticCategory.Error) continue;
    // Keep only diagnostics anchored to our source file.
    if (!d.file || d.file.fileName !== virtualName) continue;

    let line = 0;
    let column = 0;
    if (typeof d.start === 'number') {
      const pos = d.file.getLineAndCharacterOfPosition(d.start);
      line = pos.line + 1; // tsc reports 1-based
      column = pos.character + 1;
    }
    const message = ts.flattenDiagnosticMessageText(d.messageText, '\n');
    errors.push({
      line,
      column,
      message,
      detail: `line ${line}, col ${column}: ${message}`,
      code: `TS${d.code}`,
      kind,
    });
    if (errors.length >= MAX_ERRORS) break;
  }
  return errors;
}

export default function handler(req: VercelRequest, res: VercelResponse): void {
  if (req.method !== 'POST') {
    res.status(405).json({ error: 'method_not_allowed', errors: [], tool: 'tsc' });
    return;
  }

  // Fail-closed shared-secret check.
  const expected = process.env.TSC_SERVICE_SECRET;
  if (!expected) {
    res.status(503).json({ error: 'service_unconfigured', errors: [], tool: 'tsc' });
    return;
  }
  const provided = req.headers['x-tsc-secret'];
  if (provided !== expected) {
    res.status(401).json({ error: 'unauthorized', errors: [], tool: 'tsc' });
    return;
  }

  const body = (typeof req.body === 'string' ? safeParse(req.body) : req.body) || {};
  const code: unknown = body.content ?? body.code;
  const filename: unknown = body.filename ?? 'input.ts';

  if (typeof code !== 'string' || typeof filename !== 'string') {
    res.status(400).json({ error: 'bad_request', errors: [], tool: 'tsc' });
    return;
  }
  if (Buffer.byteLength(code, 'utf8') > MAX_SOURCE_BYTES) {
    res.status(413).json({ error: 'source_too_large', errors: [], tool: 'tsc' });
    return;
  }

  try {
    const errors = checkSource(code, filename);
    res.status(200).json({ errors, tool: 'tsc' });
  } catch (e) {
    // Degrade gracefully: an internal failure returns an empty error list so the
    // backend treats it as a safe SKIP (never a false block).
    res.status(200).json({ errors: [], tool: 'tsc', skipped: true, reason: String(e) });
  }
}

function safeParse(s: string): Record<string, unknown> {
  try {
    return JSON.parse(s);
  } catch {
    return {};
  }
}
