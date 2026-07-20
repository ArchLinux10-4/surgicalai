#!/usr/bin/env bash
# SurgicalAI — One-time install script
set -e

BOLD='\033[1m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
FRONTEND_DIR="$SCRIPT_DIR/frontend"

# ── Debug logging ────────────────────────────────────────────────────────────
# Every failure-prone step logs to this file (full detail), so a failed run
# can be diagnosed without reproducing it. Screen output stays clean; the
# log gets everything, including things we don't bother printing to the user.
LOGFILE="$SCRIPT_DIR/install-debug.log"
{
  echo "════════════════════════════════════════════════════"
  echo "SurgicalAI install run: $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
  echo "uname: $(uname -a 2>&1)"
  echo "whoami: $(whoami 2>&1) (uid=$(id -u 2>&1))"
  echo "shell: $SHELL / bash $BASH_VERSION"
  echo "════════════════════════════════════════════════════"
} > "$LOGFILE" 2>&1

# _dlog: append a detailed line to the log file only (not the screen)
_dlog() {
  echo "[$(date -u +'%H:%M:%S')] $*" >> "$LOGFILE"
}

# Mirror everything printed to the screen into the log too, from here on.
exec > >(tee -a "$LOGFILE") 2>&1

echo ""
echo -e "${BOLD}${BLUE}⚡ SurgicalAI — Install${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "  (full debug log: ${BLUE}${LOGFILE}${NC})"
echo ""

_dlog "SCRIPT_DIR=$SCRIPT_DIR BACKEND_DIR=$BACKEND_DIR FRONTEND_DIR=$FRONTEND_DIR"

OLLAMA_MODEL="qwen2.5-coder:7b"
OLLAMA_PORT=11434
OLLAMA_URL="http://127.0.0.1:${OLLAMA_PORT}"

# ── Check prerequisites ──────────────────────────────────────────────────────

echo -e "${BOLD}Checking prerequisites...${NC}"

# Prefer a modern interpreter — macOS system python3 is often 3.9, which
# cannot parse PEP 604 unions (dict | None) used throughout the backend.
PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" &>/dev/null; then
    PYTHON_BIN="$(command -v "$candidate")"
    break
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  _dlog "FAIL: no python3 found. PATH=$PATH"
  echo "❌ Python 3 not found. Please install Python 3.10+"
  echo "   (details logged to $LOGFILE)"
  exit 1
fi

PYTHON_VERSION=$("$PYTHON_BIN" --version 2>&1 | awk '{print $2}')
PYTHON_MAJOR=$("$PYTHON_BIN" -c 'import sys; print(sys.version_info.major)')
PYTHON_MINOR=$("$PYTHON_BIN" -c 'import sys; print(sys.version_info.minor)')
if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 10 ]; }; then
  _dlog "FAIL: Python $PYTHON_VERSION at $PYTHON_BIN is too old (need 3.10+)"
  echo "❌ Python $PYTHON_VERSION is too old. SurgicalAI requires Python 3.10+."
  echo "   Found: $PYTHON_BIN"
  echo "   Install e.g. brew install python@3.12, then re-run ./install.sh"
  echo "   (details logged to $LOGFILE)"
  exit 1
fi

_dlog "python OK: $PYTHON_VERSION ($PYTHON_BIN)"
echo "  ✅ Python $PYTHON_VERSION ($PYTHON_BIN)"

if ! command -v node &>/dev/null; then
  _dlog "FAIL: node not found. PATH=$PATH"
  echo "❌ Node.js not found. Please install Node.js 18+"
  echo "   (details logged to $LOGFILE)"
  exit 1
fi

NODE_VERSION=$(node --version)
_dlog "node OK: $NODE_VERSION ($(command -v node))"
echo "  ✅ Node.js $NODE_VERSION"

if ! command -v npm &>/dev/null; then
  _dlog "FAIL: npm not found. PATH=$PATH"
  echo "❌ npm not found."
  echo "   (details logged to $LOGFILE)"
  exit 1
fi
_dlog "npm OK: $(npm --version 2>&1) ($(command -v npm))"

echo ""

# ── Backend ──────────────────────────────────────────────────────────────────

echo -e "${BOLD}Installing backend dependencies...${NC}"
cd "$BACKEND_DIR"
_dlog "cd $BACKEND_DIR; disk free: $(df -h . 2>&1 | tail -1)"

# Drop a stale venv built with the wrong interpreter (e.g. system 3.9).
if [ -d .venv ]; then
  _dlog "removing existing .venv before recreate with $PYTHON_BIN"
  rm -rf .venv
fi

if ! "$PYTHON_BIN" -m venv .venv; then
  _dlog "FAIL: $PYTHON_BIN -m venv .venv failed"
  echo "❌ Failed to create Python virtual environment."
  echo "   (details logged to $LOGFILE)"
  exit 1
fi
source .venv/bin/activate
_dlog "venv activated: $(command -v python3), $(command -v pip)"

if ! pip install --quiet --upgrade pip; then
  _dlog "FAIL: pip self-upgrade failed"
fi
if ! pip install --quiet -r requirements.txt; then
  _dlog "FAIL: pip install -r requirements.txt. Retrying verbosely to capture the real error..."
  pip install -r requirements.txt >> "$LOGFILE" 2>&1 || true
  echo -e "  ${RED}❌ Backend dependency install failed.${NC}"
  echo "     Full pip error is in $LOGFILE"
  exit 1
fi

echo "  ✅ Backend dependencies installed"

# Local-install-only extras (e.g. Playwright for the Element Picker feature).
# requirements-local.txt is never read by Railway/hosted builds (nixpacks only
# reads requirements.txt) — this script only ever runs on a local machine, so
# it's always safe to install these here, into the same venv the app runs from.
if [ -f requirements-local.txt ]; then
  _dlog "installing local-only extras from requirements-local.txt"
  if pip install --quiet -r requirements-local.txt; then
    _dlog "requirements-local.txt installed OK"
    echo "  ✅ Local-only extras installed (Element Picker, etc.)"
  else
    _dlog "FAIL: pip install -r requirements-local.txt. Retrying verbosely to capture the real error..."
    pip install -r requirements-local.txt >> "$LOGFILE" 2>&1 || true
    echo -e "  ${YELLOW}⚠️  Local-only extras failed to install (non-fatal — core app still works).${NC}"
    echo "     Affects: Element Picker. To retry manually:"
    echo "       cd backend && source .venv/bin/activate && pip install -r requirements-local.txt"
    echo "     Full pip error is in $LOGFILE"
  fi
fi
echo ""

# ── Frontend ─────────────────────────────────────────────────────────────────

echo -e "${BOLD}Installing frontend dependencies...${NC}"
cd "$FRONTEND_DIR"
_dlog "cd $FRONTEND_DIR; disk free: $(df -h . 2>&1 | tail -1)"

if ! npm install --silent; then
  _dlog "FAIL: npm install. Retrying verbosely..."
  npm install >> "$LOGFILE" 2>&1 || true
  echo -e "  ${RED}❌ Frontend dependency install failed.${NC}"
  echo "     Full npm error is in $LOGFILE"
  exit 1
fi
echo "  ✅ Frontend dependencies installed"

echo ""
echo -e "${BOLD}Building frontend...${NC}"
if ! npm run build; then
  _dlog "FAIL: npm run build"
  echo -e "  ${RED}❌ Frontend build failed.${NC}"
  echo "     Full build error is in $LOGFILE"
  exit 1
fi
echo "  ✅ Frontend built"
echo ""

# ── Offline Mode (Ollama + Qwen2.5-coder:7b) ────────────────────────────────
# This whole section is OPTIONAL and self-contained. Any failure here is
# non-fatal to the core install — offline mode never blocks SurgicalAI itself.
# Scope (evidence-based): plain chat + whole-file rewrite only. No agent mode,
# no tool-calling, no diff/surgical edits for this model — see
# backend/services/offline/OFFLINE_MODE.md for the research behind that.

cd "$SCRIPT_DIR"
OFFLINE_OK=0

echo -e "${BOLD}Offline Mode (local AI, no cloud API needed)${NC}"
echo "SurgicalAI can run fully offline using Ollama + Qwen2.5-Coder:7b for"
echo "plain chat and whole-file rewrites — no API key, no cloud cost."
echo ""
read -r -p "Set up Offline Mode now? [y/N] " SETUP_OFFLINE
echo ""

if [[ "$SETUP_OFFLINE" =~ ^[Yy]$ ]]; then

  # Detect machine, but let the user confirm/override
  DETECTED_OS="$(uname -s)"
  if [ "$DETECTED_OS" = "Darwin" ]; then
    DETECTED_LABEL="macOS (Apple Silicon / Intel)"
  elif [ "$DETECTED_OS" = "Linux" ]; then
    DETECTED_LABEL="Linux"
  else
    DETECTED_LABEL="Unknown ($DETECTED_OS)"
  fi

  echo "  Detected: $DETECTED_LABEL"
  echo ""
  echo "  Which machine is this?"
  echo "    1) macOS (Apple Silicon, e.g. M1 Max)"
  echo "    2) Linux"
  echo "    3) Skip offline mode"
  read -r -p "  Choice [1/2/3]: " MACHINE_CHOICE
  echo ""

  case "$MACHINE_CHOICE" in
    1) TARGET_OS="Darwin" ;;
    2) TARGET_OS="Linux" ;;
    *) TARGET_OS="" ;;
  esac

  if [ -z "$TARGET_OS" ]; then
    echo -e "  ${YELLOW}Skipping offline mode setup.${NC}"
  else
    (
      set +e  # from here on: never let a failure kill the whole install

      # Helper: confirm the thing answering on the port is actually Ollama,
      # not some unrelated process squatting on 11434 (real issue: ollama#707)
      is_ollama_responding() {
        curl -fsS "$OLLAMA_URL" 2>/dev/null | grep -qi "ollama is running"
      }

      _dlog "offline: TARGET_OS=$TARGET_OS OLLAMA_URL=$OLLAMA_URL OLLAMA_MODEL=$OLLAMA_MODEL"

      # 1. Install the Ollama CLI (idempotent — skip if already present)
      if command -v ollama &>/dev/null; then
        _dlog "ollama already present: $(command -v ollama), version: $(ollama --version 2>&1)"
        echo "  ✅ Ollama already installed ($(ollama --version 2>&1 | head -1))"
      else
        echo "  Installing Ollama (official installer)..."
        _dlog "ollama not found, beginning install for TARGET_OS=$TARGET_OS"
        if [ "$TARGET_OS" = "Linux" ]; then
          echo "  (Linux installer may prompt for your sudo password — that's expected.)"
          # Real issue hit during testing: Ollama's installer needs zstd to
          # extract its archive, and it's not present by default on Debian.
          if ! command -v zstd &>/dev/null; then
            echo "  Installing missing dependency: zstd"
            _dlog "zstd missing, attempting install"
            # Use sudo only if present and we're not already root (e.g. containers)
            SUDO=""
            if [ "$(id -u)" -ne 0 ]; then
              if command -v sudo &>/dev/null; then
                SUDO="sudo"
              else
                echo "  ⚠️  Not root and no sudo found — zstd install may fail."
                _dlog "WARN: not root, no sudo found, zstd install likely to fail (uid=$(id -u))"
              fi
            fi
            if command -v apt-get &>/dev/null; then
              $SUDO apt-get update -qq && $SUDO apt-get install -y zstd
            elif command -v dnf &>/dev/null; then
              $SUDO dnf install -y zstd
            elif command -v pacman &>/dev/null; then
              $SUDO pacman -S --noconfirm zstd
            else
              _dlog "FAIL: no known package manager (apt-get/dnf/pacman) found for zstd install"
            fi
            if command -v zstd &>/dev/null; then
              _dlog "zstd install succeeded: $(command -v zstd)"
            else
              _dlog "FAIL: zstd still missing after install attempt"
            fi
          fi
        fi
        curl -fsSL https://ollama.com/install.sh | sh
        INSTALL_EXIT=$?
        if [ "$INSTALL_EXIT" -ne 0 ] || ! command -v ollama &>/dev/null; then
          _dlog "FAIL: ollama install.sh exit=$INSTALL_EXIT, command -v ollama=$(command -v ollama 2>&1)"
          _dlog "FAIL context: /usr/share/ollama exists=$([ -d /usr/share/ollama ] && echo yes || echo no), zstd present=$(command -v zstd &>/dev/null && echo yes || echo no)"
          echo -e "  ${RED}❌ Ollama install failed. Skipping offline mode.${NC}"
          if [ "$TARGET_OS" = "Linux" ] && [ ! -d /usr/share/ollama ]; then
            echo "     Known issue: installer's adduser step can silently skip"
            echo "     creating /usr/share/ollama on some distros. Try:"
            echo "       sudo mkdir -p /usr/share/ollama && sudo chown ollama:ollama /usr/share/ollama"
            echo "     then re-run: curl -fsSL https://ollama.com/install.sh | sh"
          fi
          echo "     Or install manually: https://ollama.com/download"
          echo "     Full debug detail: $LOGFILE"
          exit 1
        fi
        _dlog "ollama install succeeded: $(ollama --version 2>&1)"
        echo "  ✅ Ollama installed"
      fi

      # 2. Make sure the Ollama server is running
      if is_ollama_responding; then
        _dlog "ollama server already responding at $OLLAMA_URL"
        echo "  ✅ Ollama server already running"
      else
        # Something answering on the port but not Ollama? Bail with a clear message
        # rather than silently colliding with it (real issue: ollama#707).
        if curl -fsS "$OLLAMA_URL" &>/dev/null; then
          _dlog "FAIL: port $OLLAMA_PORT occupied by non-Ollama process. curl response: $(curl -fsS "$OLLAMA_URL" 2>&1 | head -c 300)"
          echo -e "  ${RED}❌ Port ${OLLAMA_PORT} is in use by something other than Ollama.${NC}"
          echo "     Free the port, or run Ollama on a different one:"
          echo "       OLLAMA_HOST=127.0.0.1:11435 ollama serve"
          echo "     then set that URL in Settings → Models."
          exit 1
        fi

        echo "  Starting Ollama server..."
        if [ "$TARGET_OS" = "Linux" ] && command -v systemctl &>/dev/null && [ -f /etc/systemd/system/ollama.service ]; then
          _dlog "starting via systemctl"
          sudo systemctl start ollama 2>/dev/null
          _dlog "systemctl start ollama exit=$?; status: $(systemctl is-active ollama 2>&1)"
        else
          _dlog "starting via nohup ollama serve, log at /tmp/ollama-serve.log"
          nohup ollama serve >/tmp/ollama-serve.log 2>&1 &
          disown
        fi

        READY=0
        for i in $(seq 1 20); do
          if is_ollama_responding; then
            READY=1
            break
          fi
          sleep 1
        done

        if [ "$READY" -eq 1 ]; then
          _dlog "ollama server became ready after ~${i}s"
          echo "  ✅ Ollama server is up"
        else
          _dlog "FAIL: ollama server did not respond after 20s. Last /tmp/ollama-serve.log tail:"
          _dlog "$(tail -c 2000 /tmp/ollama-serve.log 2>&1)"
          echo -e "  ${RED}❌ Ollama server did not respond after 20s. Skipping offline mode.${NC}"
          echo "     Check /tmp/ollama-serve.log for details, then run 'ollama serve' manually."
          echo "     Full debug detail: $LOGFILE"
          exit 1
        fi
      fi

      # 3. Pull the model (retry a few times — large download, flaky networks)
      if ollama list 2>/dev/null | grep -q "$OLLAMA_MODEL"; then
        _dlog "model $OLLAMA_MODEL already present: $(ollama list 2>&1 | grep "$OLLAMA_MODEL")"
        echo "  ✅ Model $OLLAMA_MODEL already present"
      else
        echo "  Pulling $OLLAMA_MODEL (this is a few GB, may take a while)..."
        _dlog "pulling $OLLAMA_MODEL, disk free: $(df -h "${HOME:-/tmp}" 2>&1 | tail -1)"
        PULL_OK=0
        for attempt in 1 2 3; do
          if ollama pull "$OLLAMA_MODEL"; then
            PULL_OK=1
            break
          fi
          _dlog "pull attempt $attempt failed, disk free: $(df -h "${HOME:-/tmp}" 2>&1 | tail -1)"
          echo "  Retry $attempt/3 failed, retrying..."
          sleep 3
        done

        if [ "$PULL_OK" -eq 1 ]; then
          _dlog "model pull succeeded on attempt $attempt"
          echo "  ✅ Model $OLLAMA_MODEL ready"
        else
          _dlog "FAIL: model pull failed after 3 attempts"
          echo -e "  ${RED}❌ Failed to pull $OLLAMA_MODEL after 3 attempts. Skipping offline mode.${NC}"
          echo "     Run manually later: ollama pull $OLLAMA_MODEL"
          echo "     Full debug detail: $LOGFILE"
          exit 1
        fi
      fi

      # 4. Final sanity check
      if ollama list 2>/dev/null | grep -q "$OLLAMA_MODEL" && is_ollama_responding; then
        _dlog "final sanity check PASSED"
        echo -e "  ${GREEN}✅ Offline mode ready — $OLLAMA_MODEL running at $OLLAMA_URL${NC}"
        exit 0
      else
        _dlog "FAIL: final sanity check failed. ollama list: $(ollama list 2>&1); responding: $(is_ollama_responding && echo yes || echo no)"
        exit 1
      fi
    )
    OFFLINE_RESULT=$?
    _dlog "offline mode subshell exit code: $OFFLINE_RESULT"
    if [ "$OFFLINE_RESULT" -eq 0 ]; then
      OFFLINE_OK=1
    fi
  fi
else
  _dlog "user declined offline mode setup"
  echo "  Skipping offline mode setup (you can enable it anytime in Settings)."
fi

echo ""
echo -e "${GREEN}${BOLD}✅ Installation complete!${NC}"
echo ""
if [ "$OFFLINE_OK" -eq 1 ]; then
  echo -e "  Offline mode: ${GREEN}ready${NC} — turn it on in Settings → Models"
  echo -e "  (base URL defaults to ${BLUE}${OLLAMA_URL}${NC}, model: ${OLLAMA_MODEL})"
else
  echo -e "  Offline mode: ${YELLOW}not set up${NC} — run this script again anytime, or install"
  echo -e "  Ollama manually and set the URL/model in Settings → Models."
  echo -e "  (if you tried and it failed, see: ${BLUE}${LOGFILE}${NC})"
fi
_dlog "install run complete. OFFLINE_OK=$OFFLINE_OK"
echo ""
echo -e "Run the app:  ${BLUE}./start.sh${NC}"
echo -e "Debug log:    ${BLUE}${LOGFILE}${NC}"
echo ""
