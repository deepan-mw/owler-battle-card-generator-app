#!/usr/bin/env bash
# List the tools the Meltwater MCP server exposes (confirms the statistics tool name).
# Reads creds from .env; run on a VPN-connected machine.
#
#   ./scripts/list_mcp_tools.sh
#
# Expected to include: unified_retrieval_statistics_retrieval_tool
# (which is the default MELTWATER_STATS_TOOL). If it differs, set:
#   export MELTWATER_MCP_STATS_TOOL=<the real name>
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

: "${MELTWATER_MCP_URL:?set MELTWATER_MCP_URL (in .env)}"
: "${MELTWATER_MCP_API_KEY:?set MELTWATER_MCP_API_KEY (in .env)}"

echo "Querying tools/list at $MELTWATER_MCP_URL ..."
RESP=$(curl -s --max-time 25 -X POST "$MELTWATER_MCP_URL" \
  -H "api-key: $MELTWATER_MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}')

# Response may be SSE (data: {json}) or plain JSON — grab tool names either way.
echo "$RESP" | grep -o '"name":"[^"]*"' | sed 's/"name":"/  - /;s/"$//' | sort -u

echo
if echo "$RESP" | grep -q '"unified_retrieval_statistics_retrieval_tool"'; then
  echo "OK: statistics tool name matches the default (MELTWATER_STATS_TOOL)."
else
  echo "NOTE: did not see 'unified_retrieval_statistics_retrieval_tool' above."
  echo "      Pick the statistics tool from the list and set:"
  echo "      export MELTWATER_MCP_STATS_TOOL=<that name>"
fi
