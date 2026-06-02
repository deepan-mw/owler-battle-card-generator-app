#!/usr/bin/env bash
# Launch the Battle Cards Generator locally: API + static UI server together.
#
#   ./scripts/run_local.sh            # API :8000, UI :5500
#   ./scripts/run_local.sh 8001 5501  # custom ports
#
# Then open: http://localhost:<UI_PORT>/frontend/index.html
# Ctrl-C stops both. Set MELTWATER_MCP_URL/_JWT[/_API_KEY] first for live statistics.
set -euo pipefail

API_PORT="${1:-8000}"
UI_PORT="${2:-5500}"

# repo root = parent of this script's dir
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

cleanup() { echo; echo "stopping..."; kill ${API_PID:-} ${UI_PID:-} 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "API  -> http://localhost:${API_PORT}  (uvicorn api:app)"
python3 -m uvicorn api:app --reload --port "${API_PORT}" &
API_PID=$!

echo "UI   -> http://localhost:${UI_PORT}/frontend/index.html  (static server)"
python3 -m http.server "${UI_PORT}" >/dev/null 2>&1 &
UI_PID=$!

# If the UI server uses a non-default API port, point the frontend at it.
if [ "${API_PORT}" != "8000" ]; then
  echo "NOTE: API on :${API_PORT} (non-default). In the browser console run:"
  echo "      localStorage.bc_api='http://localhost:${API_PORT}'; location.reload()"
fi

echo
echo "Open: http://localhost:${UI_PORT}/frontend/index.html   (Ctrl-C to stop)"
wait
