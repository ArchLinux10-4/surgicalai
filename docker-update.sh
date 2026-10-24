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
branch="$(git rev-parse --abbrev-ref HEAD)"
[ "$branch" = "main" ] || { echo "On '$branch', not main. Switch to main first."; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "Uncommitted changes. Commit or stash first."; exit 1; }
git fetch origin
git pull --ff-only origin main
echo "Rebuilding. Accounts, keys, and settings stay in the Docker volume."
./docker-up.sh
