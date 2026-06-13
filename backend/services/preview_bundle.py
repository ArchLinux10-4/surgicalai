"""Live-preview bundle resolver.

Walks the import graph of a target TSX/JSX/TS/JS file across the files that
live in the same chat session and produces a Sandpack-ready file map.

Design goals
------------
* Resolve **local** imports (relative ``./`` ``../`` and ``@/`` alias) to the
  actual sibling files in the session, recursively, so multi-file components
  render for real instead of being stubbed away.
* Keep **CSS** imports (and inline the resolved CSS as real files) — the most
  common reason a preview looks broken.
* Collect **bare npm** specifiers (e.g. ``@mui/icons-material/Login``,
  ``framer-motion``) and declare them as dependencies so the bundler installs
  them instead of erroring.
* Anything that genuinely can't be resolved is replaced with a safe stub so the
  entry still compiles and renders (graceful degradation, never a blank screen).

The result is a single source of truth that the frontend consumes verbatim,
which keeps the rendering logic identical whether previewing the original or a
modified version of a file.
"""
from __future__ import annotations

import posixpath
import re
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Tuple

# ── Extensions we try, in order, when resolving an extension-less import ──────
_RESOLVE_EXTS = [".tsx", ".ts", ".jsx", ".js", ".mjs", ".cjs", ".json", ".css"]
_INDEX_FILES = ["index.tsx", "index.ts", "index.jsx", "index.js"]
_CSS_EXTS = {".css", ".scss", ".sass", ".less"}
_ASSET_EXTS = {".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".avif", ".bmp"}

# Packages the Sandpack react-ts template already provides — never list as deps.
_PROVIDED = {"react", "react-dom", "react/jsx-runtime", "react-dom/client"}

# Mandatory peer dependencies that the in-browser bundler will NOT auto-install.
# When a key package is declared, its peers must be declared too or the package's
# own internal code fails to resolve at runtime (e.g. @mui/icons-material's
# createSvgIcon.mjs imports @mui/material). Keep this list conservative and
# limited to peers that are genuinely required for the package to load at all.
_PEER_DEPS: Dict[str, Tuple[str, ...]] = {
    "@mui/icons-material": ("@mui/material", "@emotion/react", "@emotion/styled"),
    "@mui/material": ("@emotion/react", "@emotion/styled"),
    "@mui/lab": ("@mui/material", "@emotion/react", "@emotion/styled"),
    "@mui/system": ("@emotion/react", "@emotion/styled"),
    "@mui/x-data-grid": ("@mui/material", "@emotion/react", "@emotion/styled"),
    "@mui/x-date-pickers": ("@mui/material", "@emotion/react", "@emotion/styled"),
    "@emotion/styled": ("@emotion/react",),
    "@mantine/core": ("@mantine/hooks",),
}


def expand_peer_deps(deps: Dict[str, str]) -> None:
    """Add mandatory peer dependencies for any declared package, in place."""
    # Snapshot keys first: we mutate `deps` while iterating.
    for pkg in list(deps.keys()):
        for peer in _PEER_DEPS.get(pkg, ()):  # noqa: B007
            if peer not in _PROVIDED:
                deps.setdefault(peer, "latest")

# Safety caps so a pathological graph can never hang the request.
_MAX_FILES = 80
_MAX_DEPTH = 12

# ── Import-statement matchers ────────────────────────────────────────────────
_RE_FROM = re.compile(
    r"""^[ \t]*(?P<kw>import|export)\b(?P<clause>(?:type\s+)?[\s\S]*?)\bfrom\s*['"](?P<path>[^'"]+)['"][ \t]*;?[ \t]*$""",
    re.MULTILINE,
)
_RE_SIDE = re.compile(
    r"""^[ \t]*import\s+['"](?P<path>[^'"]+)['"][ \t]*;?[ \t]*$""",
    re.MULTILINE,
)
_RE_DYNAMIC = re.compile(r"""\b(?:import|require)\s*\(\s*['"]([^'"]+)['"]\s*\)""")

_RE_HAS_DEFAULT = re.compile(r"^\s*export\s+default\b", re.MULTILINE)


def is_bare(spec: str) -> bool:
    """A bare specifier is an npm package (not relative, alias, or absolute)."""
    return not (spec.startswith(".") or spec.startswith("/") or spec.startswith("@/"))


def package_name(spec: str) -> str:
    """Return the installable npm package name for a (possibly sub-path) bare spec.

    ``@mui/icons-material/Login`` -> ``@mui/icons-material``
    ``lodash/debounce``           -> ``lodash``
    ``framer-motion``             -> ``framer-motion``
    """
    parts = spec.split("/")
    if spec.startswith("@"):
        return "/".join(parts[:2])
    return parts[0]


def _norm(path: str) -> str:
    """Normalise to a leading-slash posix path without ``.`` / ``..`` segments."""
    p = PurePosixPath("/" + path.lstrip("/"))
    out: List[str] = []
    for part in p.parts[1:]:
        if part == "." or part == "":
            continue
        if part == "..":
            if out:
                out.pop()
            continue
        out.append(part)
    return "/" + "/".join(out)


def _ext(path: str) -> str:
    return PurePosixPath(path).suffix.lower()


_DROP_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}


def _rel_import(importer_path: str, target_path: str) -> str:
    """Relative ESM specifier from ``importer_path`` to ``target_path``.

    Bundler-agnostic: works regardless of how Sandpack roots files. The script
    extension is dropped (``.tsx`` etc.); style/json extensions are preserved.
    """
    from_dir = posixpath.dirname(importer_path)
    rel = posixpath.relpath(target_path, from_dir if from_dir else "/")
    if not rel.startswith("."):
        rel = "./" + rel
    ext = _ext(rel)
    if ext in _DROP_EXT:
        rel = rel[: -len(ext)]
    return rel


def detect_component(code: str) -> str:
    """Best-effort detection of the component name to render as the entry."""
    for pat in (
        r"export\s+default\s+(?:function|class)\s+([A-Z]\w*)",
        r"export\s+default\s+([A-Z]\w*)",
        r"export\s+(?:function|class)\s+([A-Z][a-zA-Z0-9]*)",
    ):
        m = re.search(pat, code)
        if m:
            return m.group(1)
    matches = re.findall(r"(?:function|const)\s+([A-Z][a-zA-Z0-9]*)\s*[=(]", code)
    return matches[-1] if matches else "App"


def build_stub(clause: str, source: str) -> str:
    """Port of the frontend two-tier Proxy stub for unresolved named imports."""
    raw = (
        clause.replace("*", " ")
        .replace("{", " ")
        .replace("}", " ")
        .replace("type ", " ")
    )
    names: List[str] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if " as " in token:
            token = token.split(" as ")[-1].strip()
        token = token.strip()
        if re.fullmatch(r"\w+", token):
            names.append(token)
    if not names:
        return f"/* [no names to stub from: {source}] */"
    data = (
        "user|loading|isLoading|error|data|status|count|list|items|token|id|name|"
        "email|value|config|options|state|type|mode|size|length|theme|session|"
        "message|result|response|success|ready|open|visible|active|enabled|disabled|"
        "selected|checked|collapsed|expanded|setupRequired|isAuthenticated|isAdmin|"
        "isDark|isLight|isOpen|isMobile|isDesktop"
    )
    lines = []
    for n in names:
        lines.append(
            f"const {n}: any = (() => {{ "
            f"const DATA=/^({data})$/;"
            f"const mk=(t:any):any=>new Proxy(function(){{}},{{"
            f"get:(_,k)=>{{if(typeof k==='symbol')return undefined;if(k==='__esModule')return undefined;"
            f"if(k==='then'){{if(!t)return undefined;return function(r:any){{try{{r&&r({{data:{{}}}});}}catch(e){{}}return mk(true);}};}}"
            f"if(DATA.test(String(k)))return undefined;return mk(true);}},"
            f"apply:()=>mk(true),construct:()=>({{}})}});return mk(false); }})(); "
            f"/* stubbed: {source} */"
        )
    return "\n".join(lines)


def _collect_specifiers(code: str) -> List[str]:
    specs: List[str] = []
    for m in _RE_FROM.finditer(code):
        specs.append(m.group("path"))
    for m in _RE_SIDE.finditer(code):
        specs.append(m.group("path"))
    for m in _RE_DYNAMIC.finditer(code):
        specs.append(m.group(1))
    return specs


class FileIndex:
    """Case-insensitive index of session files by normalised posix path."""

    def __init__(self, files: Dict[str, str]):
        self.by_path: Dict[str, str] = {}
        self.lower_to_real: Dict[str, str] = {}
        for name, content in files.items():
            np = _norm(name)
            self.by_path[np] = content
            self.lower_to_real[np.lower()] = np

    def _lookup(self, norm_path: str) -> Optional[str]:
        if norm_path in self.by_path:
            return norm_path
        return self.lower_to_real.get(norm_path.lower())

    def resolve(self, importer_path: str, spec: str) -> Optional[str]:
        """Resolve a local import spec to a real normalised path, or None."""
        candidates: List[str] = []
        if spec.startswith("@/"):
            tail = spec[2:]
            candidates.extend([_norm(tail), _norm("src/" + tail), _norm("frontend/src/" + tail)])
        elif spec.startswith("/"):
            candidates.append(_norm(spec))
        else:  # relative
            base = str(PurePosixPath(importer_path).parent)
            candidates.append(_norm(base + "/" + spec))

        for cand in list(candidates):
            if _ext(cand):
                hit = self._lookup(cand)
                if hit:
                    return hit
            for ext in _RESOLVE_EXTS:
                hit = self._lookup(cand + ext)
                if hit:
                    return hit
            for idx in _INDEX_FILES:
                hit = self._lookup(cand + "/" + idx)
                if hit:
                    return hit

        if spec.startswith("@/"):
            tail = _norm(spec[2:]).lstrip("/")
            for ext in [""] + _RESOLVE_EXTS:
                want = "/" + tail + ext
                for np in self.by_path:
                    if np.lower().endswith(want.lower()):
                        return np
        return None


def _transform(
    code: str,
    file_path: str,
    index: "FileIndex",
    resolved_map: Dict[str, str],
    deps: Dict[str, str],
    unresolved: List[str],
    is_entry: bool,
) -> str:
    """Rewrite a single file's imports for the bundle."""

    def repl_from(m: "re.Match") -> str:
        kw = m.group("kw")
        clause = m.group("clause")
        spec = m.group("path")

        if re.match(r"\s*type\s+", clause):
            return f"/* [type import removed: {spec}] */"

        if is_bare(spec):
            # MUI icon deep imports (e.g. @mui/icons-material/Login) fail in
            # Sandpack's in-browser bundler — the package is too large and deep
            # subpath resolution breaks, leaving the import as `undefined`.
            # Stub them as lightweight SVG placeholder components instead.
            if spec.startswith("@mui/icons-material/"):
                names = re.findall(r"\b([A-Za-z_]\w*)\b", clause.split(" as ")[-1])
                local = names[0] if names else "_Icon"
                return (
                    f"import React from 'react';\n"
                    f"const {local} = (props: any) => React.createElement('svg', "
                    f"{{...props, viewBox: '0 0 24 24', "
                    f"width: props?.sx?.fontSize || props?.style?.fontSize || 24, "
                    f"height: props?.sx?.fontSize || props?.style?.fontSize || 24, "
                    f"fill: 'currentColor', "
                    f"style: {{...(props?.style || {{}}), display: 'inline-block', verticalAlign: 'middle'}}}}, "
                    f"React.createElement('rect', {{width: 18, height: 18, x: 3, y: 3, rx: 3, fill: 'currentColor', opacity: 0.18}})"
                    f");  /* icon stub: {spec} */"
                )

            pkg = package_name(spec)
            if pkg not in _PROVIDED:
                deps.setdefault(pkg, "latest")
            return m.group(0)

        target = index.resolve(file_path, spec)
        if target is not None:
            ext = _ext(target)
            if ext in _ASSET_EXTS:
                names = re.findall(r"\b([A-Za-z_]\w*)\b", clause.split(" as ")[-1])
                local = names[0] if names else "_asset"
                return f'const {local}: any = "";  /* asset stub: {spec} */'
            resolved_map.setdefault(target, "")
            return f'{kw}{clause}from "{_rel_import(file_path, target)}"'

        unresolved.append(spec)
        ext = _ext(spec)
        # An unresolved import that still binds a name must keep that binding
        # defined, or the consuming file throws a ReferenceError at runtime.
        if ext in _ASSET_EXTS:
            names = re.findall(r"\b([A-Za-z_]\w*)\b", clause.split(" as ")[-1])
            local = names[0] if names else "_asset"
            return f'const {local}: any = "";  /* unresolved asset: {spec} */'
        if ext in _CSS_EXTS:
            names = re.findall(r"\b([A-Za-z_]\w*)\b", clause.split(" as ")[-1])
            local = names[0] if names else "_styles"
            # CSS-module style binding -> proxy that returns the key as a class name
            return (
                f'const {local}: any = new Proxy({{}}, {{ get: (_: any, k: any) => '
                f'String(k) }});  /* unresolved css-module: {spec} */'
            )
        return build_stub(clause, f"unresolved: {spec}")

    def repl_side(m: "re.Match") -> str:
        spec = m.group("path")
        if is_bare(spec):
            pkg = package_name(spec)
            if pkg not in _PROVIDED:
                deps.setdefault(pkg, "latest")
            return m.group(0)
        target = index.resolve(file_path, spec)
        if target is not None:
            resolved_map.setdefault(target, "")
            return f'import "{_rel_import(file_path, target)}"'
        unresolved.append(spec)
        return f"/* [unresolved side-effect import removed: {spec}] */"

    out = _RE_FROM.sub(repl_from, code)
    out = _RE_SIDE.sub(repl_side, out)

    if "import.meta.env" in out:
        env_stub = (
            "const __import_meta_env__:any = (typeof import.meta!=='undefined' "
            "&& (import.meta as any).env) ? (import.meta as any).env : {};\n"
        )
        out = env_stub + out.replace("import.meta.env", "__import_meta_env__")

    return out


def build_bundle(
    entry_filename: str,
    entry_content: str,
    session_files: Dict[str, str],
) -> dict:
    """Build a Sandpack-ready bundle for ``entry_filename``.

    Returns a dict: {entry, files, dependencies, external, unresolved, component}.
    """
    index = FileIndex(dict(session_files))

    entry_path = _norm(entry_filename)
    deps: Dict[str, str] = {}
    unresolved: List[str] = []
    out_files: Dict[str, str] = {}

    contents: Dict[str, str] = {entry_path: entry_content}
    queue: List[Tuple[str, int]] = [(entry_path, 0)]
    seen = {entry_path}

    while queue:
        if len(out_files) >= _MAX_FILES:
            break
        path, depth = queue.pop(0)
        raw = contents.get(path)
        if raw is None:
            raw = index.by_path.get(path, "")
        resolved_map: Dict[str, str] = {}
        transformed = _transform(
            raw, path, index, resolved_map, deps, unresolved,
            is_entry=(path == entry_path),
        )
        out_files[path] = transformed

        if depth < _MAX_DEPTH:
            for target in resolved_map:
                if target not in seen:
                    seen.add(target)
                    queue.append((target, depth + 1))

    expand_peer_deps(deps)

    entry_code = out_files[entry_path]
    component = detect_component(entry_content)
    if not _RE_HAS_DEFAULT.search(entry_content):
        entry_code = f"{entry_code}\nexport default {component}\n"
        out_files[entry_path] = entry_code

    external = sorted(deps.keys())
    seen_u: Dict[str, None] = {}
    for u in unresolved:
        seen_u.setdefault(u, None)

    return {
        "entry": entry_path,
        "entryImport": _rel_import("/index.tsx", entry_path),
        "files": out_files,
        "dependencies": deps,
        "external": external,
        "unresolved": list(seen_u.keys()),
        "component": component,
    }
