#!/bin/bash
BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BACKEND_DIR"
. .venv/bin/activate
exec > "$BACKEND_DIR/uvicorn.log" 2>&1
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
