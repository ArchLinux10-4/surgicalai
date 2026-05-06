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
