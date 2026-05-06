#!/usr/bin/env bash
# SurgicalAI — Dev mode (hot reload on both backend + frontend)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
FRONTEND_DIR="$SCRIPT_DIR/frontend"

echo "⚡ SurgicalAI — Dev Mode"
echo "  Backend: http://127.0.0.1:8000"
echo "  Frontend: http://127.0.0.1:5173"
echo ""

# Kill old processes
lsof -ti:8000 | xargs kill -9 2>/dev/null || true
lsof -ti:5173 | xargs kill -9 2>/dev/null || true
sleep 0.5

# Start backend
cd "$BACKEND_DIR"
source .venv/bin/activate
uvicorn main:app --host 127.0.0.1 --port 8000 --reload &
BACKEND_PID=$!

# Start frontend dev server
cd "$FRONTEND_DIR"
npm run dev &
FRONTEND_PID=$!

echo "Both servers running. Press Ctrl+C to stop."
echo "  Open: http://127.0.0.1:5173"

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit" INT TERM
wait
