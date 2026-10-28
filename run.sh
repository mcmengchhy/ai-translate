#!/usr/bin/env bash
set -e

# Change to script directory
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$DIR"

# Check if venv exists
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
    ./.venv/bin/pip install -r requirements.txt
fi

echo "=========================================================="
echo " Starting Khmer AI Translator on http://localhost:8000"
echo "=========================================================="

./.venv/bin/python3 -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --reload
