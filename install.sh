#!/usr/bin/env bash
# SurgicalAI — One-time install script
set -e

BOLD='\033[1m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo ""
echo -e "${BOLD}${BLUE}⚡ SurgicalAI — Install${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
FRONTEND_DIR="$SCRIPT_DIR/frontend"

# ── Check prerequisites ──────────────────────────────────────────────────────

echo -e "${BOLD}Checking prerequisites...${NC}"

if ! command -v python3 &>/dev/null; then
  echo "❌ Python 3 not found. Please install Python 3.11+"
  exit 1
fi

PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "  ✅ Python $PYTHON_VERSION"

if ! command -v node &>/dev/null; then
  echo "❌ Node.js not found. Please install Node.js 18+"
  exit 1
fi

NODE_VERSION=$(node --version)
echo "  ✅ Node.js $NODE_VERSION"

if ! command -v npm &>/dev/null; then
  echo "❌ npm not found."
  exit 1
fi

echo ""

# ── Backend ──────────────────────────────────────────────────────────────────

echo -e "${BOLD}Installing backend dependencies...${NC}"
cd "$BACKEND_DIR"

python3 -m venv .venv
source .venv/bin/activate

pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo "  ✅ Backend dependencies installed"
echo ""

# ── Frontend ─────────────────────────────────────────────────────────────────

echo -e "${BOLD}Installing frontend dependencies...${NC}"
cd "$FRONTEND_DIR"

npm install --silent
echo "  ✅ Frontend dependencies installed"

echo ""
echo -e "${BOLD}Building frontend...${NC}"
npm run build
echo "  ✅ Frontend built"

echo ""
echo -e "${GREEN}${BOLD}✅ Installation complete!${NC}"
echo ""
echo -e "Run the app:  ${BLUE}./start.sh${NC}"
echo ""
