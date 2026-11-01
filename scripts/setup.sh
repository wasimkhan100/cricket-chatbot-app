#!/usr/bin/env bash
# One-time setup: models, Python deps, secrets, stats DB, vector index, UI build. Safe to re-run.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHAT_MODEL="${CRICKET_CHAT_MODEL:-qwen2.5:3b}"
THINK_MODEL="${CRICKET_THINK_MODEL:-qwen3:1.7b}"
EMBED_MODEL="${CRICKET_EMBED_MODEL:-nomic-embed-text}"

echo "==> Ollama"
if ! curl -sf http://127.0.0.1:11434/api/tags >/dev/null; then
  echo "    starting ollama serve"
  nohup ollama serve >/tmp/ollama.log 2>&1 &
  for _ in $(seq 1 30); do curl -sf http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 1; done
fi
for m in "$CHAT_MODEL" "$THINK_MODEL" "$EMBED_MODEL"; do
  if ! ollama list | awk '{print $1}' | grep -qx -e "$m" -e "$m:latest"; then ollama pull "$m"; fi
done

echo "==> Backend dependencies"
cd "$ROOT/backend"
uv sync --frozen 2>/dev/null || uv sync

if [ ! -f .env ]; then
  echo "==> Generating backend/.env with fresh secrets"
  umask 077
  cat > .env <<ENV
CRICKET_SESSION_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')
CRICKET_ADMIN_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
CRICKET_CHAT_MODEL=$CHAT_MODEL
CRICKET_THINK_MODEL=$THINK_MODEL
CRICKET_EMBED_MODEL=$EMBED_MODEL
ENV
fi

echo "==> Stats database + vector index (only changed documents are re-embedded)"
uv run python -m cricket_chatbot.ingest

echo "==> Frontend"
cd "$ROOT/frontend"
npm ci --no-audit --no-fund
npm run build

echo
echo "Done. Start with: scripts/start.sh   then open http://127.0.0.1:8000"
echo "Admin token for the System tab: $(grep CRICKET_ADMIN_TOKEN "$ROOT/backend/.env" | cut -d= -f2)"
