#!/usr/bin/env bash
# Script to immediately kill any server running on port 8000 or uvicorn processes

echo "Stopping Khmer AI Translator Server..."

PIDS=$(lsof -ti :8000 2>/dev/null)
if [ -n "$PIDS" ]; then
    echo "Killing process on port 8000 (PIDs: $PIDS)..."
    kill -9 $PIDS 2>/dev/null
    echo "✓ Port 8000 freed successfully."
else
    echo "✓ No process running on port 8000."
fi

# Kill any lingering uvicorn processes
pkill -9 -f "uvicorn backend.app.main:app" 2>/dev/null || true

echo "✓ Server is completely stopped."

