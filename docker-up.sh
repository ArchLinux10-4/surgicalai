#!/usr/bin/env bash
set -euo pipefail
_src="${BASH_SOURCE[0]}"
while [ -L "$_src" ]; do
  _dir="$(cd -P "$(dirname "$_src")" && pwd)"
  _src="$(readlink "$_src")"
  case "$_src" in
    /*) ;;
    *) _src="$_dir/$_src" ;;
  esac
done
cd "$(cd -P "$(dirname "$_src")" && pwd)"
if ! command -v docker >/dev/null 2>&1; then
  for d in "$HOME/.docker/bin" /Applications/Docker.app/Contents/Resources/bin /usr/local/bin; do
    if [ -x "$d/docker" ]; then
      export PATH="$d:$PATH"
      break
    fi
  done
fi
command -v docker >/dev/null || { echo "Install Docker Desktop first."; exit 1; }

docker volume create surgicalai-data >/dev/null

BACKUP_DIR="$HOME/.surgicalai/docker-backups"
mkdir -p "$BACKUP_DIR"
SNAP=""

# Copy the live database before the rebuild. sqlite3.backup includes the WAL,
# so a running app does not lose the last writes. Exit 2 means there is no
# database yet.
snapshot_volume() {
  local dest="$1"
  docker run --rm \
    -v surgicalai-data:/data \
    -v "$BACKUP_DIR":/backup \
    --entrypoint python \
    surgicalai:latest \
    -c '
import os, sqlite3, sys
src_path = "/data/surgicalai.db"
if not os.path.isfile(src_path):
    sys.exit(2)
src = sqlite3.connect(src_path)
dst = sqlite3.connect("/backup/" + os.path.basename(sys.argv[1]))
src.backup(dst)
dst.close()
src.close()
' "$(basename "$dest")"
}

if docker image inspect surgicalai:latest >/dev/null 2>&1; then
  SNAP="$BACKUP_DIR/surgicalai-$(date +%Y%m%d-%H%M%S).db"
  if snapshot_volume "$SNAP"; then
    echo "Saved a copy of your accounts, keys, and settings."
    ls -1t "$BACKUP_DIR"/surgicalai-*.db 2>/dev/null | tail -n +6 | while IFS= read -r old; do
      rm -f "$old"
    done || true
  else
    status=$?
    if [ "$status" -eq 2 ]; then
      SNAP=""
    else
      exit "$status"
    fi
  fi
fi

docker image inspect surgicalai:latest >/dev/null 2>&1 && docker tag surgicalai:latest surgicalai:previous || true
docker compose up -d --build

wait_healthy() {
  local i cid st
  cid="$(docker compose ps -q surgicalai)"
  [ -n "$cid" ] || { echo "Container did not start."; return 1; }
  for i in $(seq 1 30); do
    st="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || echo missing)"
    if [ "$st" = "healthy" ]; then
      return 0
    fi
    sleep 2
  done
  echo "Container did not become healthy."
  return 1
}

# Prints "users keys settings" or exits 3 when users, keys, or an existing
# setting value differ from the copy taken before the rebuild.
data_ok() {
  local snap="$1"
  docker run --rm \
    -v surgicalai-data:/data \
    -v "$BACKUP_DIR":/backup \
    --entrypoint python \
    surgicalai:latest \
    -c '
import sqlite3, sys

def load(path):
    con = sqlite3.connect(path)
    users = con.execute(
        "select id, username, hashed_password, is_admin, is_active from users order by id"
    ).fetchall()
    keys = con.execute(
        "select user_id, key_type, encrypted_value from user_api_keys order by user_id, key_type"
    ).fetchall()
    settings = dict(con.execute("select key, value from settings").fetchall())
    con.close()
    return users, keys, settings

old = load(sys.argv[1])
new = load(sys.argv[2])
lost = []
if old[0] != new[0]:
    lost.append("users")
if old[1] != new[1]:
    lost.append("keys")
for key, value in old[2].items():
    if new[2].get(key) != value:
        lost.append(key)
        break
print(len(new[0]), len(new[1]), len(new[2]))
if lost:
    sys.stderr.write("changed: " + ", ".join(lost) + "\n")
    sys.exit(3)
' "/backup/$(basename "$snap")" /data/surgicalai.db
}

restore_snapshot() {
  local snap="$1"
  docker compose stop
  docker run --rm \
    -v surgicalai-data:/data \
    -v "$BACKUP_DIR":/backup \
    --entrypoint python \
    surgicalai:latest \
    -c '
import os, sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
src.close()
for suffix in ("-wal", "-shm"):
    try:
        os.remove(sys.argv[2] + suffix)
    except FileNotFoundError:
        pass
' "/backup/$(basename "$snap")" /data/surgicalai.db
  docker compose up -d --no-build
}

wait_healthy

if [ -n "$SNAP" ]; then
  set +e
  report="$(data_ok "$SNAP" 2>"$BACKUP_DIR/last-check.err")"
  code=$?
  set -e
  if [ "$code" -eq 3 ]; then
    echo "The rebuild changed saved accounts, keys, or settings. Restoring the copy from before the rebuild."
    restore_snapshot "$SNAP"
    wait_healthy
    set +e
    report="$(data_ok "$SNAP" 2>"$BACKUP_DIR/last-check.err")"
    code=$?
    set -e
    if [ "$code" -ne 0 ]; then
      echo "Restore did not bring the saved data back. Backup: $SNAP"
      exit 1
    fi
  elif [ "$code" -ne 0 ]; then
    echo "Could not check saved accounts and keys. Backup: $SNAP"
    exit "$code"
  fi
  read -r users keys settings <<<"$report"
  user_word="users"; [ "$users" = 1 ] && user_word="user"
  key_word="saved keys"; [ "$keys" = 1 ] && key_word="saved key"
  setting_word="settings"; [ "$settings" = 1 ] && setting_word="setting"
  echo "Kept ${users} ${user_word}, ${keys} ${key_word}, and ${settings} ${setting_word}."
  rm -f "$BACKUP_DIR/last-check.err"
fi

echo "SurgicalAI: http://127.0.0.1:${SURGICALAI_PORT:-8000}"
