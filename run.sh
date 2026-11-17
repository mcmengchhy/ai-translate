#!/usr/bin/env bash
set -e

# Change to script directory
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$DIR"

# Check if venv exists
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

# Keep existing environments up to date when dependencies change.
./.venv/bin/python3 -m pip install -r requirements.txt

echo "=========================================================="
echo " Starting Khmer AI Translator on http://localhost:8000"
echo "=========================================================="

server_args=(backend.app.main:app --host 0.0.0.0 --port 8000)
# Reloading Python during a long render cancels its background work.
if [ "${DEV_RELOAD:-0}" = "1" ]; then
    server_args+=(--reload)
fi
exec ./.venv/bin/python3 -m uvicorn "${server_args[@]}"
