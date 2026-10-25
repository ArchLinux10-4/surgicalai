"""Plan build: stage files from the open folder, keep a new symbol, search safely."""
import os
import sys

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)


def test_new_symbol_region_keeps_a_matching_anchor():
    from services.text_region_promotion import new_symbol_region

    src = "OPENAI_MODEL = \"gpt-4.1\"\n\ndef helper():\n    return 1\n"
    symbol, new_code, ok, reason = new_symbol_region(
        src,
        "OPENAI_MODEL = \"gpt-4.1\"\n",
        "OPENAI_MODEL = \"gpt-4.1\"\nMODEL_CATALOG = {}\n",
        "config.py",
        "MODEL_CATALOG",
    )
    assert ok is True
    assert reason == "new_symbol_file_level"
    assert symbol.name == "MODEL_CATALOG"
    assert symbol.code.startswith("OPENAI_MODEL")
    assert "MODEL_CATALOG" in new_code


def test_new_symbol_region_rejects_an_ambiguous_anchor():
    from services.text_region_promotion import new_symbol_region

    src = "x = 1\nx = 1\n"
    symbol, _new, ok, reason = new_symbol_region(src, "x = 1\n", "x = 2\n", "a.py", "Y")
    assert symbol is None and ok is False and reason == "old_code_not_unique"


def test_stage_planned_file_from_the_open_folder(tmp_path, monkeypatch):
    import database as db
    from services.repo_apply import stage_planned_files

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(db, "USE_POSTGRES", False)
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "stage.db"))
    db.init_db()
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (folder / "config.py").write_text("NAME = 'a'\n", encoding="utf-8")
    sid = "sess-1"
    with db.get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, title, user_id) VALUES (?, ?, ?)",
            (sid, "t", "user-a"),
        )
        conn.execute(
            "INSERT INTO repositories (id, user_id, name, root_path) VALUES (?, ?, ?, ?)",
            ("repo-1", "user-a", "proj", str(folder)),
        )
        conn.commit()
    repo = {"id": "repo-1", "root_path": str(folder), "user_id": "user-a"}
    staged = stage_planned_files(sid, "user-a", repo, ["app.py", "missing.py"])
    assert [row["filename"] for row in staged] == ["app.py"]
    assert "def run" in staged[0]["content"]
    with db.get_db_ctx() as conn:
        row = conn.execute(
            "SELECT filename FROM session_files WHERE session_id = ?", (sid,),
        ).fetchone()
    assert row["filename"] == "app.py"
    disk = (folder / "app.py").read_text(encoding="utf-8")
    assert disk.startswith("def run")


def test_correction_prompt_states_the_insert_rule_for_a_new_symbol():
    from services.pipeline import _build_symbol_correction

    text = _build_symbol_correction(
        [{
            "filename": "config.py",
            "symbol": "MODEL_CATALOG",
            "old_code": "NAME = 'a'\n",
            "new_code": "NAME = 'a'\nMODEL_CATALOG = {}\n",
            "description": "add the catalog",
        }],
        {},
    )
    assert "is not in config.py" in text
    assert "To add something new" in text


def test_folder_search_skips_a_large_binary(tmp_path):
    from services.repo_tools import _search

    (tmp_path / "app.py").write_text("MODEL_CATALOG = {}\n", encoding="utf-8")
    blob = tmp_path / "title_classifier_model.pkl"
    blob.write_bytes(b"\x00pickle")
    os.truncate(blob, 50 * 1024 * 1024)
    text = _search(str(tmp_path), ["MODEL_CATALOG"])
    assert "app.py" in text
    assert "MODEL_CATALOG" in text
    assert "pkl" not in text


def test_folder_search_skips_logs_and_does_not_say_stop(tmp_path):
    from services.repo_tools import _search

    page = tmp_path / "templates"
    page.mkdir()
    (page / "index.html").write_text('<option value="gpt-4.1">\n', encoding="utf-8")
    (tmp_path / "api_call_log.jsonl").write_text("gpt-4.1\n" * 4000, encoding="utf-8")
    text = _search(str(tmp_path), ["gpt-4.1"])
    assert "index.html" in text
    assert "jsonl" not in text
    assert "stop" not in text.lower()
    assert "cut short" not in text.lower()


def test_folder_search_can_limit_to_a_path(tmp_path):
    from services.repo_tools import _search

    page = tmp_path / "templates"
    page.mkdir()
    (page / "index.html").write_text("gpt-4.1\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("gpt-4.1\n", encoding="utf-8")
    text = _search(str(tmp_path), ["gpt-4.1"], "templates")
    assert "templates/index.html" in text
    assert "other.py" not in text


def test_long_read_names_the_next_line(tmp_path):
    from services.repo_tools import _read_window

    content = "\n".join(f"L{i:04d}" + ("x" * 200) for i in range(1, 3000))
    start, end, total, body, continues = _read_window(content, 100)
    assert start == 100
    assert continues
    assert end < total
    assert body.startswith("L0100")
    assert not body.startswith("L0001")
