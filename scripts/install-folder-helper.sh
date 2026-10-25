#!/usr/bin/env bash
# Install the per-user LaunchAgent that opens the macOS folder dialog for
# SurgicalAI running in Docker. Idempotent: safe to run on every ./docker-rebuild-local.sh.
# Native installs do not need this; the backend calls osascript itself.
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  echo "Folder helper is only used on macOS."
  exit 0
fi

_src="${BASH_SOURCE[0]}"
while [ -L "$_src" ]; do
  _dir="$(cd -P "$(dirname "$_src")" && pwd)"
  _src="$(readlink "$_src")"
  case "$_src" in
    /*) ;;
    *) _src="$_dir/$_src" ;;
  esac
done
SCRIPT_DIR="$(cd -P "$(dirname "$_src")" && pwd)"
HELPER="$SCRIPT_DIR/macos-folder-helper.py"
[ -f "$HELPER" ] || { echo "Missing $HELPER"; exit 1; }

PY=""
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [ -x "$candidate" ]; then
    PY="$candidate"
    break
  fi
done
[ -n "$PY" ] || { echo "python3 not found. Install Xcode Command Line Tools or Homebrew python3."; exit 1; }

STATE_DIR="$HOME/.surgicalai"
mkdir -p "$STATE_DIR"
TOKEN_FILE="$STATE_DIR/folder-helper-token"
if [ ! -s "$TOKEN_FILE" ]; then
  umask 077
  "$PY" -c 'import secrets; print(secrets.token_urlsafe(32))' > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE"

LABEL="com.surgicalai.folder-helper"
AGENTS_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS_DIR"
PLIST="$AGENTS_DIR/$LABEL.plist"
LOG="$STATE_DIR/folder-helper.log"
DOMAIN="gui/$(id -u)"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PY}</string>
    <string>${HELPER}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>LimitLoadToSessionType</key>
  <string>Aqua</string>
  <key>ProcessType</key>
  <string>Interactive</string>
  <key>StandardOutPath</key>
  <string>${LOG}</string>
  <key>StandardErrorPath</key>
  <string>${LOG}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>SURGICALAI_PICKER_TOKEN_FILE</key>
    <string>${TOKEN_FILE}</string>
    <key>SURGICALAI_PICKER_PORT</key>
    <string>18765</string>
  </dict>
</dict>
</plist>
EOF

launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
launchctl enable "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "$DOMAIN" "$PLIST"

ready=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if "$PY" -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:18765/health", timeout=1).read()' >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.25
done

if [ "$ready" -ne 1 ]; then
  echo "Folder helper did not start. See $LOG"
  exit 1
fi

echo "Folder picker helper is running."
