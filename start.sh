#!/usr/bin/env bash
# Launcher for the AI Case Sorter OSS client.
#
# Usage:
#   ./start.sh                Launch the client (prompt before any sudo).
#   ./start.sh --auto         Auto-confirm any sudo apt/dnf/pacman installs.
#   AUTO_INSTALL=1 ./start.sh Same as --auto, for non-interactive shells.
#   ./start.sh --setup-only   Install uv and the dependencies, then exit
#                             without starting the app.
#
# All the actual bootstrap logic (Python, uv, dependency sync, staged
# updates) lives in bootstrap.py -- this file just needs *some* Python 3 to
# hand off to it. See bootstrap.py's module docstring for why it's Python
# and not more shell.
set -euo pipefail
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
    exec python3 bootstrap.py "$@"
elif command -v python >/dev/null 2>&1; then
    exec python bootstrap.py "$@"
else
    echo "[start] ERROR: no python3/python found on PATH." >&2
    echo "[start] Install Python 3 (any reasonably recent version) and re-run." >&2
    exit 1
fi
