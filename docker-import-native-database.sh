#!/usr/bin/env bash
# One time only, when moving from a non-Docker install to Docker.
# Copies ~/.surgicalai/surgicalai.db into the Docker volume.
# Does not rebuild, and refuses to overwrite a database already in Docker.
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
command -v sqlite3 >/dev/null || { echo "sqlite3 is required to copy the database safely."; exit 1; }
SRC="$HOME/.surgicalai/surgicalai.db"
[ -f "$SRC" ] || { echo "No native database at $SRC"; exit 1; }
docker volume create surgicalai-data >/dev/null
if docker run --rm -v surgicalai-data:/d alpine sh -c 'test -f /d/surgicalai.db'; then
  echo "Volume already has a database. Not overwriting."
  exit 1
fi
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
sqlite3 "$SRC" ".backup '$tmp/surgicalai.db'"
docker run --rm -v surgicalai-data:/d -v "$tmp":/src alpine cp /src/surgicalai.db /d/surgicalai.db
echo "Migrated. Native copy is untouched."
