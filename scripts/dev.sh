#!/usr/bin/env bash
# Frontend hot reload on http://127.0.0.1:5173 (proxies /api to the backend on :8000). Run scripts/start.sh first.
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)/frontend"
exec npm run dev
