# ⚡ SurgicalAI

**A local-first AI coding assistant and dev-ops control center.**
Surgical, AST-aware code edits, multi-step autonomous agent runs, a fully offline mode, and built-in panels for GitHub, Railway, Vercel, and Linear — all running on your own machine.

---

## Features

### Coding modes
- **Full chat** — a regular AI coding assistant chat with file/project context.
- **Surgical mode** — the AI reads a file's AST symbol map (not raw text), plans an exact change, and returns only the modified block. Every change shows a diff and a confidence score before it touches disk. File backups are taken automatically before every write.
- **Agent mode** — multi-step autonomous task planning and execution (Claude models only). Tasks are tracked per session/run, can be cancelled individually or all at once, and show live progress in a dedicated Mission Control panel.
- **Whole-file rewrite** — used automatically when Offline Mode is active (see below).

### Models
- Cloud: Claude (Sonnet 5, Opus, Haiku) and OpenAI (GPT-5.x family, o-series, GPT-4.1) — bring your own API key, stored encrypted in your local database (never in a cloud secret store).
- **Offline Mode** — run entirely without a cloud API key using **Ollama + Qwen2.5-Coder:7b**. Fully isolated codebase (`backend/services/offline/`) that never touches the Claude/OpenAI pipeline; falls back automatically only when no cloud key is configured. Scoped deliberately to **plain chat + whole-file rewrite** — agent mode, tool-calling, and diff-style edits are evidence-backed as unreliable at the 7B scale and are intentionally not attempted offline.

### Multi-language AST support
- **Python** — native `ast` module (gold-standard accuracy).
- **JavaScript / TypeScript / JSX / TSX** — `tree-sitter`, with automatic regex-based error recovery when the grammar chokes on unusual syntax, and a full regex fallback if `tree-sitter` isn't available.
- Also supported: Go, Rust, Java, HTML, CSS, Markdown — plus a generic fallback for any other file type.

### Multi-user & auth
- First run creates an admin account; admins can create additional users, reset passwords, and see online/idle/offline presence.
- JWT-based sessions, bcrypt password hashing, self-service password change with complexity rules enforced server-side.

### Integrations (token-based, no OAuth app setup required)
Paste a personal API token in Settings for any of these — it's encrypted and stored per-user in your local database:
- **GitHub** — native PAT integration, plus a GitHub App flow for repo access.
- **Railway** — projects, deployments, and build/runtime logs via Railway's GraphQL API.
- **Vercel** — projects, deployments, and deployment logs via Vercel's REST API.
- **Linear** — search/list issues, view details, comment, and mark issues done.
- **Deploy Watcher** — polls your latest Vercel/Railway deployment and automatically extracts error lines from failed build logs.

### Other tools
- **Image Studio** — GPT-image generation and editing (multi-turn), via OpenAI's Responses API.
- **DataLab** — natural-language spreadsheet/CSV transforms (feature-flagged).
- Monaco-based code editor, Git panel (status/diff/commit), Live Preview, Test Runner panel, session file tray, and one-click session/all-file downloads.
- Voice input for chat.

### Data & privacy
- SQLite by default; automatically uses Postgres if `DATABASE_URL` is set.
- API keys and integration tokens are encrypted and stored in your own database — never sent anywhere except the relevant provider (OpenAI, Anthropic, GitHub, Railway, Vercel, Linear).
- File backups stored at `.surgicalai_backups/` next to each modified file.

---

## Requirements

- Python 3 with `pip`
- Node.js with `npm`
- (Optional) [Ollama](https://ollama.com) — only needed if you want Offline Mode

`install.sh` verifies these are present before doing anything else.

---

## Install

```bash
cd surgicalai
chmod +x install.sh start.sh start-dev.sh
./install.sh
```

This will:
1. Create a Python virtual environment and install backend dependencies
2. Install frontend dependencies and build the production bundle
3. Optionally set up **Offline Mode** (interactive — asks whether you're on macOS Apple Silicon or Linux, then installs Ollama, starts its server, and pulls `qwen2.5-coder:7b`)

Every step logs full detail to `install-debug.log`, so a failed run can be diagnosed without reproducing it. Offline Mode setup is entirely optional and non-fatal — declining it, or a failure during it, never blocks the rest of the install.

Supported platforms: **Debian Linux** and **macOS (Apple Silicon)**.

---

## Run

**Production mode** (uses the built frontend):
```bash
./start.sh
```

**Dev mode** (hot reload on both backend + frontend):
```bash
./start-dev.sh
```

---

## First-Time Setup

1. Open the app — the first visit prompts you to create an admin account.
2. Go to **Settings** and either:
   - Add an OpenAI and/or Anthropic API key, or
   - Turn on **Offline Mode** if you ran the Ollama setup during install.
3. (Optional) Connect GitHub, Railway, Vercel, and/or Linear from their Settings panels by pasting a personal API token — no OAuth app registration needed.
4. Open a project folder from the sidebar and start chatting, or switch to **Surgical** mode for AST-aware targeted edits, or use **Agent mode** for multi-step autonomous tasks (Claude models only).

---

## Project Structure

```
surgicalai/
├── backend/
│   ├── main.py                        # FastAPI entry point
│   ├── database.py                    # SQLite/Postgres, settings, users, chat history
│   ├── auth_utils.py / crypto_utils.py # JWT + encrypted key storage
│   ├── requirements.txt
│   ├── models/schemas.py              # Pydantic models
│   ├── services/
│   │   ├── ast_parser.py              # Multi-language AST/symbol-map parser
│   │   ├── pipeline.py                # Architect + Surgeon pipeline (Claude/GPT)
│   │   ├── surgical_editor.py         # Applies validated changes to files
│   │   ├── task_planner.py / task_runner.py  # Agent mode (Claude-only)
│   │   ├── offline/                   # Isolated Offline Mode (Ollama/Qwen2.5-Coder)
│   │   ├── datalab/                   # Spreadsheet/CSV transform engine
│   │   ├── github_app_auth.py, github_context_tools.py, github_natural_tag.py
│   │   ├── deploy_status.py
│   │   └── git_service.py
│   └── routers/
│       ├── auth.py                    # Login, setup, admin user management
│       ├── chat.py                    # Chat sessions + streaming
│       ├── surgical.py / tasks.py     # Surgical apply / agent task control
│       ├── files.py / session_files.py
│       ├── git.py / github.py / github_app.py
│       ├── railway.py / vercel.py / linear.py / deploy_watch.py
│       ├── images.py                  # Image Studio
│       ├── datalab.py
│       └── settings.py
├── frontend/
│   └── src/
│       ├── App.tsx, stores/appStore.ts (Zustand), types/index.ts
│       └── components/
│           ├── Layout.tsx, Sidebar.tsx, ChatPanel.tsx, CodePanel.tsx
│           ├── SurgicalPanel.tsx, DiffView.tsx, InlineDiffCard.tsx
│           ├── AgentMissionControl.tsx, TaskListPanel.tsx
│           ├── GitHubPanel.tsx, GitHubAppPanel.tsx, GitHubCommitModal.tsx
│           ├── RailwayPanel.tsx, VercelPanel.tsx, LinearPanel.tsx
│           ├── DeployStatusPanel.tsx, DeployWatcher.tsx
│           ├── ImageStudio.tsx, DataLabModal.tsx, LivePreview.tsx
│           ├── TestRunnerPanel.tsx, AdminUsersPanel.tsx, SettingsModal.tsx
│           └── SessionFilesTray.tsx, DownloadAllButton.tsx
├── install.sh
├── start.sh
└── start-dev.sh
```

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

**API key issues:** Settings → re-enter and verify your key for the provider you're using.

**Port 8000 in use:**
```bash
kill $(lsof -ti:8000) && ./start.sh
```

**Offline Mode setup failed:** re-run `./install.sh` any time, or check `install-debug.log` for the exact error (port conflicts, missing `zstd`, or a slow model pull are the most common causes).

**Integration not connecting (GitHub/Railway/Vercel/Linear):** make sure the pasted token has the right scopes for that provider, and check Settings → the integration's panel for the specific error returned by their API.
