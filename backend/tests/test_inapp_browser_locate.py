"""DOM node to session-file scoring."""
from services.inapp_browser_locate import locate_in_files, stable_classes

INDEX = """<html><body>
<h1>Choose a model</h1>
<select id="gptModel" class="model-select p-4"></select>
</body></html>
"""
CSS = ".model-select { color: red; }\n.p-4 { padding: 4px; }\n"
WIDGET = 'export function Widget(){ return <div className="model-select">x</div> }\n'
NOISE = '<div class="p-4 mt-2 w-1/2">nope</div>\n'
APP = "export function App() {\n  return null\n}\nexport const line = 4\n"


def _files():
    return [
        {"id": "1", "filename": "index.html", "content": INDEX, "file_type": "code"},
        {"id": "2", "filename": "styles.css", "content": CSS, "file_type": "code"},
        {"id": "3", "filename": "Widget.tsx", "content": WIDGET, "file_type": "code"},
        {"id": "4", "filename": "noise.html", "content": NOISE, "file_type": "code"},
        {"id": "5", "filename": "shot.png", "content": "zzzz", "file_type": "image"},
    ]


def test_id_text_and_class_rank_index_first_and_ignore_tailwind():
    assert "p-4" not in stable_classes(["model-select", "p-4", "Button_title_abc12"])
    node = {
        "tag": "select",
        "el_id": "gptModel",
        "classes": ["model-select", "p-4", "Button_title_abc12"],
        "text": "Choose a model",
    }
    hits = locate_in_files(_files(), node)
    assert hits[0]["file"] == "index.html"
    assert hits[0]["line"] == 3
    assert hits[0]["file_id"] == "1"
    assert "noise.html" not in [hit["file"] for hit in hits]
    assert "shot.png" not in [hit["file"] for hit in hits]
    names = [hit["file"] for hit in hits]
    assert names.index("index.html") < names.index("styles.css")
    assert hits[0]["score"] > hits[1]["score"]


def test_react_dev_source_scores_highest():
    files = _files() + [{"id": "9", "filename": "src/App.tsx", "content": APP, "file_type": "code"}]
    hits = locate_in_files(files, {
        "tag": "div",
        "react": {"file": "/Users/me/src/App.tsx", "line": 2, "component": "App"},
    })
    assert hits[0]["file"] == "src/App.tsx"
    assert hits[0]["line"] == 2
    assert hits[0]["score"] >= 100


def test_jinja_text_does_not_score():
    hits = locate_in_files(
        [{"id": "1", "filename": "t.html", "content": "<p>{{ Choose a model }}</p>\n", "file_type": "code"}],
        {"tag": "p", "text": "Choose a model"},
    )
    assert hits == []
