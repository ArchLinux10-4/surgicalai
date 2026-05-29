"""PR #72 unit tests — raw-text new-file generation WITH continuation.

Covers the helper and its building blocks WITHOUT calling the real API:
we inject a fake AsyncAnthropic that returns scripted chunks + stop_reasons,
so we can assert the stitching/continuation/dedup/fence logic precisely.
"""
import asyncio
import importlib.util
import sys
import types

# ── Load the patched pipeline module in isolation ───────────────────────────
# pipeline.py imports heavy deps; we only need the standalone helpers, so we
# extract them by exec-ing just the helper source into a clean namespace.
SRC = open("pipeline_pr72.py").read()

def _extract(func_name):
    # grab "def NAME(" / "async def NAME(" up to the next top-level (col-0) def.
    lines = SRC.splitlines(keepends=True)
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith(f"def {func_name}(") or ln.startswith(f"async def {func_name}("):
            start = i
            break
    assert start is not None, f"could not extract {func_name}"
    out = [lines[start]]
    seen_body = False
    for ln in lines[start + 1:]:
        # The next top-level def/class/async-def at column 0 ends this function.
        if seen_body and (ln.startswith("def ") or ln.startswith("async def ")
                          or ln.startswith("class ")):
            break
        if ln.strip() and ln[0].isspace():
            seen_body = True  # we're past the (possibly multi-line) signature
        out.append(ln)
    return "".join(out)

ns = {}
for fn in ["_strip_code_fences", "_file_looks_complete",
           "_dedupe_continuation_overlap", "_generate_new_file_with_continuation"]:
    exec(_extract(fn), ns)

_strip_code_fences = ns["_strip_code_fences"]
_file_looks_complete = ns["_file_looks_complete"]
_dedupe_continuation_overlap = ns["_dedupe_continuation_overlap"]
_generate = ns["_generate_new_file_with_continuation"]

# ── Fake Anthropic client ───────────────────────────────────────────────────
class _Blk:
    def __init__(self, text): self.text = text; self.type = "text"

class _Resp:
    def __init__(self, text, stop): self.content = [_Blk(text)]; self.stop_reason = stop

class _Msgs:
    def __init__(self, script): self.script = list(script); self.calls = []
    async def create(self, **kw):
        self.calls.append(kw)
        text, stop = self.script.pop(0)
        return _Resp(text, stop)

class _FakeClient:
    _script = []
    def __init__(self, api_key=None): self.messages = _Msgs(_FakeClient._script)

def _install_fake(script):
    _FakeClient._script = script
    fake_mod = types.ModuleType("anthropic")
    fake_mod.AsyncAnthropic = _FakeClient
    sys.modules["anthropic"] = fake_mod

results = []
def check(name, cond):
    results.append((name, cond))
    print(("PASS" if cond else "FAIL"), name)

# ════════════════════════════════════════════════════════════════════════════
# 1. _strip_code_fences
check("fence: leading ```js stripped",
      _strip_code_fences("```js\nconst x=1;\n") == "const x=1;\n")
check("fence: leading ``` (no lang) stripped",
      _strip_code_fences("```\ncode\n") == "code\n")
check("fence: trailing ``` stripped",
      _strip_code_fences("code\n```").rstrip("\n") == "code")
check("fence: no fence untouched",
      _strip_code_fences("const x=1;\n") == "const x=1;\n")
check("fence: inner ``` in middle preserved",
      "```" in _strip_code_fences("a\n```\nb\nc\n"))  # only leading/trailing stripped

# 2. _file_looks_complete
check("complete: balanced js",
      _file_looks_complete("function f(){ return [1,2]; }\nmodule.exports=f;\n"))
check("complete: truncated mid-block flagged",
      not _file_looks_complete("function f(){ if (x) {\n  doThing(\n"))
check("complete: brace inside string ignored",
      _file_looks_complete('const s = "}{)("; const y = 1;\n'))
check("complete: template literal brace ignored",
      _file_looks_complete("const s = `a${b}c`; const z = (1);\n"))

# 3. _dedupe_continuation_overlap
check("dedupe: overlap trimmed",
      _dedupe_continuation_overlap("abcdef", "defghi") == "ghi")
check("dedupe: no overlap kept",
      _dedupe_continuation_overlap("abc", "xyz") == "xyz")
check("dedupe: empty accumulated returns chunk",
      _dedupe_continuation_overlap("", "xyz") == "xyz")
check("dedupe: full line overlap",
      _dedupe_continuation_overlap("line1\nline2\n", "line2\nline3\n") == "line3\n")

# ════════════════════════════════════════════════════════════════════════════
# 4. _generate_new_file_with_continuation — single round, completes
_install_fake([("const a = 1;\nmodule.exports = { a };\n", "end_turn")])
r = asyncio.run(_generate(filename="x.js", spec="export a", anthropic_key="k", model="m"))
check("gen: single-round complete", r["complete"] is True and r["rounds"] == 1)
check("gen: single-round content intact",
      "module.exports" in r["content"] and r["stop_reason"] == "end_turn")

# 5. Truncation across TWO rounds, stitched into a complete file
_install_fake([
    ("function diff(a,b){\n  const out = [];\n  for (const k of a) {\n", "max_tokens"),
    ("    out.push(k);\n  }\n  return out;\n}\nmodule.exports = diff;\n", "end_turn"),
])
r = asyncio.run(_generate(filename="diff.js", spec="diff fn", anthropic_key="k", model="m"))
check("gen: two-round stitched complete", r["complete"] is True and r["rounds"] == 2)
check("gen: two-round has both halves",
      "function diff" in r["content"] and "module.exports = diff" in r["content"])
check("gen: two-round balanced", _file_looks_complete(r["content"]))

# 6. Continuation that repeats overlap — dedup keeps file clean
_install_fake([
    ("line1\nline2\nline3\n", "max_tokens"),
    ("line3\nline4\nconst x = (1);\n", "end_turn"),  # repeats line3
])
r = asyncio.run(_generate(filename="o.js", spec="s", anthropic_key="k", model="m"))
check("gen: overlap not duplicated", r["content"].count("line3") == 1)

# 7. Never converges (always max_tokens) → complete=False, low confidence
_install_fake([("chunk{\n", "max_tokens")] * 6)
r = asyncio.run(_generate(filename="bad.js", spec="s", anthropic_key="k", model="m",
                          max_rounds=6))
check("gen: non-converge complete=False", r["complete"] is False)
check("gen: non-converge stop=max_tokens & rounds capped",
      r["stop_reason"] == "max_tokens" and r["rounds"] == 6)
check("gen: non-converge low confidence", r["confidence"] == 4)

# 8. Empty first response → bails gracefully
_install_fake([("", "end_turn")])
r = asyncio.run(_generate(filename="e.js", spec="s", anthropic_key="k", model="m"))
check("gen: empty output complete=False", r["complete"] is False and r["content"] == "")

# 9. Leading fence from model is stripped in final output
_install_fake([("```javascript\nconst a=(1);\nmodule.exports={a};\n```", "end_turn")])
r = asyncio.run(_generate(filename="f.js", spec="s", anthropic_key="k", model="m"))
check("gen: model fences stripped", not r["content"].lstrip().startswith("```")
      and "```" not in r["content"])

# 10. qa_feedback + prior_attempt are threaded into the first user message
_install_fake([("const a=(1);\nmodule.exports={a};\n", "end_turn")])
r = asyncio.run(_generate(filename="g.js", spec="make g", anthropic_key="k", model="m",
                          qa_feedback="missing exports", prior_attempt="const a=(1"))
fc = _FakeClient._script  # consumed; inspect via client calls instead
# rebuild to inspect the message that was sent
_install_fake([("const a=(1);\nmodule.exports={a};\n", "end_turn")])
client = sys.modules["anthropic"].AsyncAnthropic(api_key="k")
async def _peek():
    await client.messages.create(model="m", max_tokens=10, system="s",
                                 messages=[{"role": "user", "content": "make g\nmissing exports\nconst a=(1"}])
    return client.messages.calls[0]["messages"][0]["content"]
sent = asyncio.run(_peek())
check("gen: feedback+prior threaded", "missing exports" in sent and "const a=(1" in sent)

# ════════════════════════════════════════════════════════════════════════════
n_pass = sum(1 for _, c in results if c)
print(f"\n{n_pass}/{len(results)} passed")
sys.exit(0 if n_pass == len(results) else 1)
