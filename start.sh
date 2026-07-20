#!/usr/bin/env bash
# SurgicalAI — Start script
# Starts backend (FastAPI) and opens the app in your browser.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
FRONTEND_DIR="$SCRIPT_DIR/frontend"

BLUE='\033[0;34m'
GREEN='\033[0;32m'
BOLD='\033[1m'
NC='\033[0m'

echo ""
echo -e "${BOLD}${BLUE}⚡ SurgicalAI${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Check if venv exists
if [ ! -d "$BACKEND_DIR/.venv" ]; then
  echo "❌ Not installed. Run ./install.sh first."
  exit 1
fi

# Kill any existing process on port 8000
if lsof -Pi :8000 -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "  Stopping previous backend on :8000..."
  kill $(lsof -Pi :8000 -sTCP:LISTEN -t) 2>/dev/null || true
  sleep 1
fi

echo -e "  Starting backend at ${BLUE}http://127.0.0.1:8000${NC}"

cd "$BACKEND_DIR"
source .venv/bin/activate

# ── Full shutdown on Ctrl+C ──────────────────────────────────────────────────
# Ctrl+C alone does NOT clean everything up:
#  - The Element Picker's debug Chrome is launched with start_new_session=True
#    (so it survives `uvicorn --reload` restarts), which detaches it from this
#    script's process group. SIGINT never reaches it on its own.
#  - Ollama (offline mode) is started independently by install.sh (nohup, or a
#    systemd service on Linux) and isn't part of this script's process tree at
#    all.
# This trap stops the backend, the picker's debug Chrome, and Ollama so
# nothing is left running after Ctrl+C.
PICKER_PROFILE_DIR="$HOME/.surgicalai/picker-chrome-profile"

cleanup() {
  trap - INT TERM  # avoid re-entering cleanup if a second Ctrl+C arrives
  echo ""
  echo "Shutting down SurgicalAI (stopping all related processes)..."

  # 1. Backend (and anything still bound to :8000 after a graceful TERM)
  if [ -n "$BACKEND_PID" ] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill -TERM "$BACKEND_PID" 2>/dev/null
  fi
  sleep 0.3
  if lsof -Pi :8000 -sTCP:LISTEN -t >/dev/null 2>&1; then
    kill -9 $(lsof -Pi :8000 -sTCP:LISTEN -t) 2>/dev/null || true
  fi

  # 2. Element Picker debug Chrome — matched by its unique profile dir so we
  #    never touch the user's real Chrome.
  if pgrep -f -- "--user-data-dir=$PICKER_PROFILE_DIR" >/dev/null 2>&1; then
    echo "  Stopping Element Picker debug Chrome..."
    pkill -9 -f -- "--user-data-dir=$PICKER_PROFILE_DIR" 2>/dev/null || true
  fi

  # 3. Ollama (offline mode) — stop it too, even though install.sh started it
  #    independently, so Ctrl+C leaves zero leftover processes.
  #    Matched by exact process name (-x), not a free-text `pkill -f` pattern:
  #    `-f` matches full command lines and can catch unrelated processes whose
  #    argv merely contains the substring "ollama serve" (e.g. another
  #    terminal running install.sh, or a log-tail command) — confirmed with a
  #    live test where `pkill -f "ollama serve"` killed an unrelated process.
  if command -v systemctl &>/dev/null && systemctl is-active --quiet ollama 2>/dev/null; then
    echo "  Stopping Ollama (offline mode, systemd service)..."
    sudo -n systemctl stop ollama 2>/dev/null || pkill -x ollama 2>/dev/null || true
  elif pgrep -x ollama >/dev/null 2>&1; then
    echo "  Stopping Ollama (offline mode)..."
    pkill -x ollama 2>/dev/null || true
  fi

  echo "  ✅ All processes stopped."
  exit 0
}
trap cleanup INT TERM

uvicorn main:app --host 127.0.0.1 --port 8000 --reload &
BACKEND_PID=$!

# Wait for backend to be ready
echo "  Waiting for backend..."
for i in {1..20}; do
  if curl -s http://127.0.0.1:8000/api/health > /dev/null 2>&1; then
    echo -e "  ${GREEN}✅ Backend ready${NC}"
    break
  fi
  sleep 0.5
done

echo ""
echo -e "${GREEN}${BOLD}✅ SurgicalAI is running!${NC}"
echo ""
echo -e "  🌐 Open: ${BLUE}http://127.0.0.1:8000${NC}"
echo ""
echo "  Press Ctrl+C to stop."
echo ""

# Open browser (works on macOS, Linux with xdg-open)
if command -v open &>/dev/null; then
  sleep 0.5 && open "http://127.0.0.1:8000" &
elif command -v xdg-open &>/dev/null; then
  sleep 0.5 && xdg-open "http://127.0.0.1:8000" &
fi

# Keep running until Ctrl+C
wait $BACKEND_PID
