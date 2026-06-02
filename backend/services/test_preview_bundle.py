"""Unit tests for the preview bundle resolver."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from preview_bundle import build_bundle, package_name, is_bare, detect_component, _norm

PASS = 0
FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


print("== helpers ==")
check("bare react", is_bare("react"))
check("not bare relative", not is_bare("./x"))
check("not bare alias", not is_bare("@/x"))
check("pkg scoped subpath", package_name("@mui/icons-material/Login") == "@mui/icons-material",
      package_name("@mui/icons-material/Login"))
check("pkg lodash subpath", package_name("lodash/debounce") == "lodash")
check("pkg plain", package_name("framer-motion") == "framer-motion")
check("norm dotdot", _norm("a/b/../c") == "/a/c", _norm("a/b/../c"))
check("detect default fn", detect_component("export default function Hero(){}") == "Hero")
check("detect named const", detect_component("const Widget = () => {}\n") == "Widget")


print("\n== multi-file graph: relative + css + alias + npm ==")
files = {
    "src/pages/Landing.tsx": (
        "import React from 'react'\n"
        "import { Hero } from './components/Hero'\n"
        "import Nav from '../layout/Nav'\n"
        "import './landing.css'\n"
        "import { Button } from '@/ui/Button'\n"
        "import { motion } from 'framer-motion'\n"
        "export default function Landing(){ return <div><Nav/><Hero/><Button/></div> }\n"
    ),
    "src/pages/components/Hero.tsx": (
        "import './hero.css'\n"
        "export function Hero(){ return <h1>Hi</h1> }\n"
    ),
    "src/pages/components/hero.css": ".hero{color:red}",
    "src/pages/landing.css": ".landing{padding:10px}",
    "src/layout/Nav.tsx": "export default function Nav(){ return <nav/> }",
    "src/ui/Button.tsx": "export function Button(){ return <button/> }",
}
b = build_bundle("src/pages/Landing.tsx", files["src/pages/Landing.tsx"], files)

check("entry path", b["entry"] == "/src/pages/Landing.tsx", b["entry"])
check("Hero included", "/src/pages/components/Hero.tsx" in b["files"])
check("Nav included (../)", "/src/layout/Nav.tsx" in b["files"])
check("Button included (@/ alias)", "/src/ui/Button.tsx" in b["files"])
check("landing.css included", "/src/pages/landing.css" in b["files"])
check("hero.css included (transitive)", "/src/pages/components/hero.css" in b["files"])
check("framer-motion is a dep", b["dependencies"].get("framer-motion") == "latest", b["dependencies"])
check("react NOT a dep", "react" not in b["dependencies"])
check("no unresolved", b["unresolved"] == [], b["unresolved"])
# entry should import siblings via relative specifiers (bundler-agnostic)
ent_landing = b["files"]["/src/pages/Landing.tsx"]
check("entry imports Hero relatively", '"./components/Hero"' in ent_landing, ent_landing)
check("entry imports Nav relatively (../)", '"../layout/Nav"' in ent_landing, ent_landing)
check("entry imports Button via rel (alias resolved)", '"../ui/Button"' in ent_landing, ent_landing)
check("css side-effect import kept (rel, ext preserved)", '"./landing.css"' in ent_landing, ent_landing)
check("entryImport from index harness", b["entryImport"] == "./src/pages/Landing", b["entryImport"])


print("\n== unresolved local import becomes a stub (graceful) ==")
files2 = {
    "App.tsx": (
        "import { Missing } from './nope'\n"
        "import logo from './logo.svg'\n"
        "export default function App(){ return <div>{logo}<Missing/></div> }\n"
    )
}
b2 = build_bundle("App.tsx", files2["App.tsx"], files2)
ent = b2["files"]["/App.tsx"]
check("missing stubbed", "const Missing" in ent and "Proxy" in ent, ent[:200])
check("svg asset stubbed empty", 'const logo' in ent and '""' in ent)
check("./nope flagged unresolved", "./nope" in b2["unresolved"], b2["unresolved"])
check("entry still has default export", "export default" in ent)


print("\n== entry without default export gets one appended ==")
files3 = {"Card.tsx": "export function Card(){ return <div/> }"}
b3 = build_bundle("Card.tsx", files3["Card.tsx"], files3)
check("appended default export Card", "export default Card" in b3["files"]["/Card.tsx"],
      b3["files"]["/Card.tsx"])


print("\n== import.meta.env neutralised ==")
files4 = {"E.tsx": "const x = import.meta.env.VITE_API\nexport default function E(){return <i/>}"}
b4 = build_bundle("E.tsx", files4["E.tsx"], files4)
check("env neutralised", "__import_meta_env__" in b4["files"]["/E.tsx"])
check("no raw import.meta.env left", "import.meta.env" not in b4["files"]["/E.tsx"])


print("\n== type import removed ==")
files5 = {"T.tsx": "import type { Foo } from './types'\nexport default function T(){return <i/>}"}
b5 = build_bundle("T.tsx", files5["T.tsx"], files5)
check("type import removed", "type import removed" in b5["files"]["/T.tsx"])
check("Foo not stubbed", "const Foo" not in b5["files"]["/T.tsx"])


print("\n== cycle safety (A<->B) ==")
files6 = {
    "A.tsx": "import { b } from './B'\nexport default function A(){return <div/>}",
    "B.tsx": "import { a } from './A'\nexport const b = 1",
}
b6 = build_bundle("A.tsx", files6["A.tsx"], files6)
check("cycle: both included", "/A.tsx" in b6["files"] and "/B.tsx" in b6["files"])


print("\n== REAL test subject: LandingPage.tsx ==")
with open("/agent/uploads/LandingPage.tsx") as f:
    lp = f.read()
b7 = build_bundle("LandingPage.tsx", lp, {"LandingPage.tsx": lp})
check("mui icons declared as dep", b7["dependencies"].get("@mui/icons-material") == "latest",
      b7["dependencies"])
check("react not a dep", "react" not in b7["dependencies"])
check("no unresolved (self-contained)", b7["unresolved"] == [], b7["unresolved"])
check("entry present", b7["entry"] == "/LandingPage.tsx")
check("default export ensured", "export default" in b7["files"]["/LandingPage.tsx"])
# the embedded CSS template literal must be untouched
check("embedded sai- CSS preserved", ".sai-nav{" in b7["files"]["/LandingPage.tsx"])
check("mui import preserved verbatim", "@mui/icons-material/Login" in b7["files"]["/LandingPage.tsx"])
# peer deps: @mui/icons-material needs @mui/material + emotion or it fails at runtime
check("mui/material peer declared", b7["dependencies"].get("@mui/material") == "latest", b7["dependencies"])
check("emotion/react peer declared", b7["dependencies"].get("@emotion/react") == "latest", b7["dependencies"])
check("emotion/styled peer declared", b7["dependencies"].get("@emotion/styled") == "latest", b7["dependencies"])


print("\n== peer-dep expansion (isolated) ==")
from preview_bundle import expand_peer_deps
d = {"@mui/icons-material": "latest"}
expand_peer_deps(d)
check("icons->material", d.get("@mui/material") == "latest")
check("icons->emotion react", d.get("@emotion/react") == "latest")
check("icons->emotion styled", d.get("@emotion/styled") == "latest")
d2 = {"framer-motion": "latest"}
expand_peer_deps(d2)
check("no spurious peers", list(d2.keys()) == ["framer-motion"], d2)
d3 = {"@emotion/styled": "latest"}
expand_peer_deps(d3)
check("styled->react chain", d3.get("@emotion/react") == "latest")


print(f"\n==== {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
