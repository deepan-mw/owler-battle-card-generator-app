#!/usr/bin/env python3
"""Capture the first REAL Media statistics responses and verify the mapper guess.

Reads creds from the environment, calls the live statistics transport for one domain
(volume + sentiment), saves each raw response into fixtures/, and prints what
`_map_statistics` parsed out of it — so we can confirm the guessed envelope shape
(`aggregations.by_time` / `by_sentiment`) before relying on it.

Usage:
    export MELTWATER_MCP_URL=...  MELTWATER_MCP_JWT=...  [MELTWATER_MCP_API_KEY=...]
    python3 scripts/capture_live_stats.py [domain]      # default: salesforce.com

Nothing is fabricated: with missing creds it exits with a clear message and writes
no files.
"""
from __future__ import annotations
import asyncio, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import aggregator as A

_FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")


async def _capture(domain: str) -> int:
    url = os.environ.get("MELTWATER_MCP_URL")
    jwt = os.environ.get("MELTWATER_MCP_JWT")
    api_key = os.environ.get("MELTWATER_MCP_API_KEY")
    if not (url and jwt):
        print("ERROR: set MELTWATER_MCP_URL and MELTWATER_MCP_JWT (and optionally "
              "MELTWATER_MCP_API_KEY) before running. No files written.", file=sys.stderr)
        return 2

    jobs = {
        "volume": A.make_http_statistics_fn(url, jwt, api_key,
                                            query_builder=A.build_volume_query),
        "sentiment": A.make_http_statistics_fn(url, jwt, api_key,
                                               query_builder=A.build_sentiment_query),
    }
    captured: dict[str, object] = {}
    for kind, fn in jobs.items():
        try:
            raw = await fn(domain)
        except Exception as exc:  # network / auth / shape — surface, don't swallow
            print(f"[{kind}] live call FAILED: {exc}", file=sys.stderr)
            continue
        path = os.path.join(_FIX, f"live_stats_{kind}_{domain}.json")
        with open(path, "w") as fh:
            json.dump(raw, fh, indent=2)
        captured[kind] = raw
        print(f"[{kind}] saved raw response -> {path}")

    if not captured:
        print("No responses captured.", file=sys.stderr)
        return 1

    # Verify the mapper against what actually came back.
    parsed = A._map_statistics(captured.get("volume"), captured.get("sentiment"))
    print("\n_map_statistics parsed:")
    print(json.dumps(parsed, indent=2))
    if parsed.get("volume_trend") is None:
        print("\nNOTE: volume_trend is None — the live envelope shape likely differs "
              "from the guess. Inspect the saved fixture(s) and adjust `_find_buckets` "
              "/ `_map_statistics` to match, then re-run.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    dom = sys.argv[1] if len(sys.argv) > 1 else "salesforce.com"
    raise SystemExit(asyncio.run(_capture(dom)))
