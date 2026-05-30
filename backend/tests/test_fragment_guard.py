"""
Regression guard for the degenerate-fragment detector that prevents a small
snippet (supplied without old_code) from destructively replacing a large symbol.
"""
import re, types

# Load just _fragment_reason from pipeline.py without importing the whole module.
src = open("pipeline.py").read()
m = re.search(r"\ndef _fragment_reason\(.*?\n(?=\ndef |\n# ---)", src, re.S)
assert m, "could not locate _fragment_reason"
ns = {}
exec("def _fragment_reason" + m.group(0).split("def _fragment_reason",1)[1], ns)
_fragment_reason = ns["_fragment_reason"]

def big_symbol(n=850):
    lines = ["export function LandingPage() {"]
    for i in range(n-2):
        lines.append(f"  const [f{i}, setF{i}] = useState('');")
    lines.append("}")
    return "\n".join(lines)

SYM = big_symbol(850)

def test_two_line_fragment_flagged():
    frag = '        <p className="hero-sub">New</p>\n        <button>Go</button>'
    r = _fragment_reason(SYM, frag)
    assert r and "FRAGMENT" in r, r

def test_full_symbol_with_decl_not_flagged():
    # genuine full re-emit (keeps declaration) — even if reformatted shorter-ish
    full = SYM  # identical, has declaration
    assert _fragment_reason(SYM, full) is None

def test_large_refactor_keeping_decl_not_flagged():
    # shrinks a lot but keeps the declaration line -> legitimate, not flagged
    refactor = "export function LandingPage() {\n  return <div/>;\n}"
    assert _fragment_reason(SYM, refactor) is None

def test_small_symbol_never_flagged():
    small = "function add(a,b){\n  return a+b;\n}"  # 3 lines < 40
    assert _fragment_reason(small, "return 1;") is None

def test_fragment_missing_decl_but_not_much_smaller_not_flagged():
    # new_code missing decl but ~same size -> not a fragment (full-ish rewrite)
    body = "\n".join(f"  line{i}" for i in range(600))
    assert _fragment_reason(SYM, body) is None

def test_medium_symbol_fragment_flagged():
    sym = "def handler(req, res):\n" + "\n".join(f"    step{i}()" for i in range(80))
    frag = "    step5_modified()"
    assert _fragment_reason(sym, frag) is not None
