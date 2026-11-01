#!/usr/bin/env bash
# Start Ollama if needed, then the API + UI on http://127.0.0.1:8000
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if ! curl -sf http://127.0.0.1:11434/api/tags >/dev/null; then
  echo "starting ollama serve"
  nohup ollama serve >/tmp/ollama.log 2>&1 &
  for _ in $(seq 1 30); do curl -sf http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 1; done
fi
cd "$ROOT/backend"
exec uv run python -m cricket_chatbot.main
