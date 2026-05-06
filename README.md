# ⚡ SurgicalAI

**Local AI coding assistant with surgical precision.**  
Built for developers who work with large codebases and need AI that actually understands what it's changing.

---

## Features

- **Dual-model pipeline** — Architect (GPT-5/4o) plans, Surgeon (GPT-4.1) executes with minimal hallucination
- **Surgical code editor** — AST-aware changes to specific functions/classes without touching anything else
- **Diff review** — Always see exactly what changes before applying
- **Confidence scoring** — Every change rated 1–10; low-confidence changes flagged for review
- **Auto-backup** — File backup before every change, one-click restore
- **Full chat mode** — Regular coding assistant chat with file context
- **Monaco editor** — VS Code-grade editor built in
- **Git panel** — Status, diff, commit from within the app
- **Multi-language** — Python, JS/TS, Go, Rust, Java, C/C++, Ruby, PHP, Swift, Kotlin, and more
- **100% local** — All data stays on your machine. SQLite DB at `~/.surgicalai/`

---

## Requirements

- Python 3.11+
- Node.js 18+
- npm

---

## Install

```bash
cd surgicalai
chmod +x install.sh start.sh start-dev.sh
./install.sh
```

---

## Run

**Production mode** (uses built frontend, fastest):
```bash
./start.sh
```

**Dev mode** (hot reload on both backend + frontend):
```bash
./start-dev.sh
```

Then open: **http://127.0.0.1:8000** (production) or **http://127.0.0.1:5173** (dev)

---

## First-Time Setup

1. Open the app → click ⚙️ Settings (top-left)
2. Enter your OpenAI API key and click **Verify**
3. Set your workspace folder path
4. Configure your models:
   - **Architect**: `gpt-4o` or `o4-mini` for planning (better reasoning)
   - **Surgeon**: `gpt-4.1` for writing code (lowest hallucination)

---

## How Surgical Mode Works

1. Open a file from the sidebar
2. Switch to **✂️ Surgical** mode in the chat
3. Describe the change: _"Add input validation to the create_user function"_
4. The **Architect** reads the file's symbol map → plans exactly what to change
5. The **Surgeon** receives only the target code block + plan → writes the replacement
6. Review the diff, check confidence scores, click **Apply**

### Best Practices (baked in)

1. **Read the map before touching the territory** — Architect works from AST symbol map, not raw code
2. **Minimal footprint** — Surgeon only returns the target block, validated against the AST
3. **Verify before commit** — Every change shows a diff + confidence score before writing to disk

---

## Project Structure

```
surgicalai/
├── backend/
│   ├── main.py                    # FastAPI entry point
│   ├── database.py                # SQLite (settings, chat, history)
│   ├── requirements.txt
│   ├── models/
│   │   └── schemas.py             # Pydantic models
│   ├── services/
│   │   ├── ast_parser.py          # Multi-language AST parser
│   │   ├── pipeline.py            # Architect + Surgeon pipeline
│   │   ├── surgical_editor.py     # Apply changes to files
│   │   └── git_service.py         # Git operations
│   └── routers/
│       ├── settings.py            # API key management
│       ├── chat.py                # Chat sessions + messages
│       ├── files.py               # File browser + read/write
│       ├── surgical.py            # Analyze + apply changes
│       └── git.py                 # Git status/commit
├── frontend/
│   ├── src/
│   │   ├── App.tsx
│   │   ├── api/client.ts          # Typed API client
│   │   ├── stores/appStore.ts     # Zustand state
│   │   ├── types/index.ts
│   │   └── components/
│   │       ├── Layout.tsx
│   │       ├── Sidebar.tsx        # File tree + chat sessions
│   │       ├── ChatPanel.tsx      # Chat + surgical mode
│   │       ├── CodePanel.tsx      # Monaco editor + tabs
│   │       ├── SurgicalPanel.tsx  # Change review
│   │       ├── DiffView.tsx       # Before/after diff
│   │       └── SettingsModal.tsx  # API key + model config
│   └── package.json
├── install.sh
├── start.sh
└── start-dev.sh
```

---

## Data Storage

All data is local at `~/.surgicalai/surgicalai.db`:
- API key (stored in SQLite, never sent to any service except OpenAI)
- Chat sessions and history
- Surgical change history
- Settings

File backups stored at `.surgicalai_backups/` next to each modified file.

---

## Keyboard Shortcuts

- `Ctrl+Enter` — Send message
- `Ctrl+S` — Save file (in editor)

---

## Troubleshooting

**Backend won't start:**
```bash
cd backend && python3 -m pip install -r requirements.txt
```

**Frontend not loading:**
```bash
cd frontend && npm install && npm run build
```

**API key issues:**
- Visit Settings → re-enter and verify your key
- Make sure your key has GPT-4.1 / GPT-4o access

**Port 8000 in use:**
```bash
kill $(lsof -ti:8000) && ./start.sh
```
