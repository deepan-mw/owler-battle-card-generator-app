"""Owler Battlecard Engine - aggregation service.

Domain in -> parallel fetch from 4 sources -> normalize -> LLM synthesis
-> schema-valid battlecard JSON, with a 24h in-memory cache.

Source status (this build):
  - Media (Meltwater unified_retrieval): REAL response mapping wired. Transport is
    an injectable async hook (`retrieval_fn`); default replays a captured live-data
    fixture, falls back to mock on any failure. In production `retrieval_fn` becomes
    the HTTP call to the retrieval API (the MCP tool is session-bound and cannot be
    invoked from this process).
  - Owler / GenAI Lens / Gong: connectors not yet available. Each adapter is shaped
    as real-call-or-fallback; the real call is a one-line swap once wired (see TODO).

LLM synthesis is a real prompt-per-section chain with a confidence guardrail. The
model transport is an injectable hook (`llm_fn`); default = a deterministic
synthesizer so the service runs end-to-end with no credentials. Swap `llm_fn` for
an Anthropic/gateway call in production.

Output conforms to battlecard.schema.json.
"""
from __future__ import annotations
import asyncio, json, time, datetime as dt, os, re
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

CACHE_TTL = 86400  # 24h
CONFIDENCE_FLOOR = 0.5  # sections below this are regenerated once


# --- Cache backends -------------------------------------------------------
# Pluggable card cache so the engine survives restart/scale-out when backed by
# Redis, yet runs with zero infra by default. Backend is chosen once from env:
# REDIS_URL set + `redis` importable + reachable -> Redis (native TTL via SETEX),
# else an in-memory dict. All cache ops are best-effort: a Redis hiccup degrades
# to a cache miss / no-op, never an error in the request path.
_CACHE_NS = "battlecard:v1:"


class _InMemoryCache:
    """Process-local cache (does not survive restart or scale beyond one process)."""
    def __init__(self) -> None:
        self._d: dict[str, tuple[float, dict]] = {}

    def get(self, key: str):
        hit = self._d.get(key)
        if not hit:
            return None
        ts, card = hit
        if time.time() - ts >= CACHE_TTL:
            self._d.pop(key, None)
            return None
        return card

    def set(self, key: str, card: dict) -> None:
        self._d[key] = (time.time(), card)


class _RedisCache:
    """Redis-backed cache with native key expiry. Serializes cards as JSON.
    Every call is wrapped so a connection error behaves as a miss / no-op."""
    def __init__(self, client) -> None:
        self._r = client

    def get(self, key: str):
        try:
            raw = self._r.get(_CACHE_NS + key)
        except Exception:
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def set(self, key: str, card: dict) -> None:
        try:
            self._r.setex(_CACHE_NS + key, CACHE_TTL, json.dumps(card, default=str))
        except Exception:
            pass


_cache_backend = None  # resolved lazily on first use


def _build_cache():
    url = os.environ.get("REDIS_URL")
    if url:
        try:
            import redis  # optional dependency; only needed for the Redis backend
            client = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
            client.ping()  # fail fast -> fall back to in-memory
            return _RedisCache(client)
        except Exception:
            pass  # unreachable/misconfigured Redis -> degrade to in-memory
    return _InMemoryCache()


def get_cache():
    """Return the active cache backend, building it once from env."""
    global _cache_backend
    if _cache_backend is None:
        _cache_backend = _build_cache()
    return _cache_backend


def set_cache(backend) -> None:
    """Override the cache backend (test hook / explicit wiring). Pass None to reset."""
    global _cache_backend
    _cache_backend = backend
_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
# Demo mode reads credible, real-shaped sample responses captured from the four
# source tools (provided by QA). Mapped through the SAME mappers as live data, so
# wiring real connectors later is a no-op. Tagged `_demo` -> labeled "(sample data)".
_DEMO_DIR = os.path.join(_FIXTURE_DIR, "demo")

# Injectable transports. Override in tests / production.
#   retrieval_fn(domain) -> raw unified_retrieval envelope (list[{"type","text"}])
#   llm_fn(prompt) -> model completion string (expected to contain a JSON object)
RetrievalFn = Callable[[str], Awaitable[Any]]
SourceFn = Callable[[str], Awaitable[Any]]  # domain -> raw source response (owler/genai/gong)
LlmFn = Callable[[str], str]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_usd(n: Any) -> str | None:
    """Format a raw USD figure as a compact string (schema wants e.g. '$34.9B')."""
    if not isinstance(n, (int, float)):
        return None
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            return f"${n / div:.1f}{suf}".replace(".0", "")
    return f"${n:.0f}"


def _domain_to_name(d: Any) -> Any:
    """Turn a competitor domain ('hubspot.com') into a display name ('Hubspot')."""
    if not isinstance(d, str) or not d:
        return d
    base = d.split("//")[-1].split("/")[0].split(".")
    sld = base[-2] if len(base) >= 2 else base[0]
    return sld.replace("-", " ").title()


# --- Media: real unified_retrieval response mapping -----------------------

async def _fixture_retrieval(domain: str) -> Any:
    """Default media transport: replay a captured live unified_retrieval response.

    Production swap: replace this with the HTTP call to the retrieval API, e.g.
        resp = await http.post(RETRIEVAL_URL, json={"query": f"{domain} company news",
               "platforms": ["news"], "startDate": ..., "endDate": ...}, headers=auth)
        return resp.json()   # same envelope shape this parser already handles
    """
    path = os.path.join(_FIXTURE_DIR, f"media_{domain}.json")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def build_media_query(domain: str, vs: str | None = None, days: int = 7) -> dict:
    """Construct the unified_retrieval request params for a competitor's news.

    Returns the kwargs the retrieval tool/endpoint expects. Centralized so the
    fixture transport, the HTTP transport, and tests all agree on the query.
    """
    company = domain.split(".")[0]
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    return {"query": f"{company} company news announcements products competitors",
            "platforms": ["news"], "startDate": start.isoformat(),
            "endDate": end.isoformat(), "limit": 10}


def build_volume_query(domain: str, days: int = 14) -> dict:
    """statistics_retrieval params for a daily/weekly document-count series, used
    to derive news_feed.volume_trend (rising vs falling coverage)."""
    company = domain.split(".")[0]
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    return {"query": f"{company} document count by week", "platforms": ["news"],
            "startDate": start.isoformat(), "endDate": end.isoformat(), "limit": 50}


def build_sentiment_query(domain: str, days: int = 14) -> dict:
    """statistics_retrieval params for a sentiment breakdown, used to refine
    news_feed.sentiment_summary with real aggregate counts (not per-article heuristics)."""
    company = domain.split(".")[0]
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    return {"query": f"{company} sentiment breakdown", "platforms": ["news"],
            "startDate": start.isoformat(), "endDate": end.isoformat(), "limit": 50}


def make_http_statistics_fn(base_url: str, token: str, api_key: str | None = None,
                            query_builder: Callable[[str], dict] = build_volume_query) -> "RetrievalFn":
    """Production statistics transport: POST to the same MCP/HTTP endpoint and return
    the envelope `_map_statistics` handles. Credentials come from env vars, never
    hardcoded; e.g.
        sfn = make_http_statistics_fn(os.environ["MELTWATER_MCP_URL"],
                                      os.environ["MELTWATER_MCP_JWT"],
                                      os.environ.get("MELTWATER_MCP_API_KEY"))
    """
    async def _fn(domain: str) -> Any:
        import httpx
        headers = {"Authorization": f"Bearer {token}"}
        if api_key:
            headers["api-key"] = api_key
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(base_url, json=query_builder(domain), headers=headers)
            resp.raise_for_status()
            return resp.json()
    return _fn


# Tool names on the Meltwater MCP server (override via env if they differ in prod).
MELTWATER_STATS_TOOL = "unified_retrieval_statistics_retrieval_tool"
MELTWATER_DOC_TOOL = "unified_retrieval_document_retrieval_tool"


def _parse_mcp_response(status_code: int, content_type: str, text: str) -> dict:
    """Parse an MCP Streamable-HTTP reply, which may be plain JSON or an SSE stream
    (`data: {...}` lines). Returns the JSON-RPC object (with `result` or `error`)."""
    is_sse = ("text/event-stream" in (content_type or "")
              or text.lstrip().startswith(("event:", "data:")) or "\ndata:" in text)
    if is_sse:
        last = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                try:
                    obj = json.loads(line[5:].strip())
                except Exception:
                    continue
                if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                    last = obj
        if last is not None:
            return last
    return json.loads(text)


def make_mcp_tool_fn(base_url: str, token: str | None = None, api_key: str | None = None, *,
                     tool_name: str, arg_builder: Callable[[str], dict]) -> "RetrievalFn":
    """Production transport for an MCP Streamable-HTTP server (e.g. the Meltwater MCP at
    .../v1/internal/mcp). Performs the JSON-RPC handshake (initialize → initialized) then
    `tools/call` with `arg_builder(domain)`, and returns the tool result `content`
    (`[{"type":"text","text":"<json>"}]`) so the existing `_unwrap_envelope` /
    `_map_statistics` parsing handles it unchanged.

    Auth: the Meltwater MCP authenticates on the `api-key` (APIM subscription key) alone —
    confirmed live 2026-06-02. The Bearer `token` is optional and only sent if provided.

        sfn = make_mcp_tool_fn(os.environ["MELTWATER_MCP_URL"],
                               api_key=os.environ["MELTWATER_MCP_API_KEY"],
                               tool_name=MELTWATER_STATS_TOOL,
                               arg_builder=build_volume_query)
    Credentials come from env vars, never hardcoded.
    """
    async def _fn(domain: str) -> Any:
        import httpx
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if api_key:
            headers["api-key"] = api_key
        async with httpx.AsyncClient(timeout=30) as client:
            # 1) initialize (some servers are stateless; best-effort session capture)
            init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "battlecard-generator", "version": "1.0"}}}
            r = await client.post(base_url, json=init, headers=headers)
            sid = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
            if sid:
                headers["Mcp-Session-Id"] = sid
            try:  # 2) initialized notification (ignored by stateless servers)
                await client.post(base_url, headers=headers,
                                  json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            except Exception:
                pass
            # 3) tools/call
            call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arg_builder(domain)}}
            r = await client.post(base_url, json=call, headers=headers)
            r.raise_for_status()
            data = _parse_mcp_response(r.status_code, r.headers.get("content-type", ""), r.text)
            if "error" in data:
                raise RuntimeError(f"MCP tool {tool_name} error: {data['error']}")
            result = data.get("result", data)
            if isinstance(result, dict) and "content" in result:
                return result["content"]  # [{"type":"text","text": "<json>"}]
            return result
    return _fn


def make_http_retrieval_fn(base_url: str, token: str,
                           query_builder: Callable[[str], dict] = build_media_query) -> "RetrievalFn":
    """Production media transport: POST to the retrieval HTTP API and return the
    same envelope shape `_parse_retrieval_envelope` already handles.

    Usage:
        rfn = make_http_retrieval_fn(os.environ["RETRIEVAL_URL"], os.environ["MW_TOKEN"])
        await generate_battlecard("salesforce.com", retrieval_fn=rfn)
    """
    async def _fn(domain: str) -> Any:
        import httpx  # lazy: only needed when this transport is selected
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(base_url, json=query_builder(domain),
                                     headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            return resp.json()
    return _fn


def _default_source_query(domain: str) -> dict:
    """Minimal request body for the source MCPs (Owler/GenAI Lens/Gong). The exact
    shape is unconfirmed until provisioning — adjust per the real API, same as the
    statistics envelope guess. The corresponding `_map_*` handles the RESPONSE shape."""
    return {"domain": domain}


def make_http_source_fn(base_url: str, token: str, api_key: str | None = None,
                        query_builder: Callable[[str], dict] = _default_source_query
                        ) -> "SourceFn":
    """Generic production transport for the per-source MCPs (Owler / GenAI Lens / Gong):
    POST {query_builder(domain)} with Bearer auth (+ optional api-key header) and return
    the raw response for the matching `_map_*`. Credentials come from env vars, never
    hardcoded; e.g.
        ofn = make_http_source_fn(os.environ["OWLER_MCP_URL"], os.environ["OWLER_MCP_JWT"])
        await generate_battlecard("salesforce.com", owler_fn=ofn)
    """
    async def _fn(domain: str) -> Any:
        import httpx  # lazy: only needed when this transport is selected
        headers = {"Authorization": f"Bearer {token}"}
        if api_key:
            headers["api-key"] = api_key
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(base_url, json=query_builder(domain), headers=headers)
            resp.raise_for_status()
            return resp.json()
    return _fn


def _unwrap_envelope(envelope: Any) -> dict:
    """Unwrap an MCP tool envelope ([{"type":"text","text":"<json>"}], a dict, or a
    JSON string) into a plain dict payload. Shared by document + statistics parsing."""
    if isinstance(envelope, list) and envelope and isinstance(envelope[0], dict) \
            and "text" in envelope[0]:
        return json.loads(envelope[0]["text"])
    if isinstance(envelope, dict):
        return envelope
    return json.loads(envelope)


def _parse_retrieval_envelope(envelope: Any) -> list[dict]:
    """Unwrap the unified_retrieval MCP envelope into a list of resource dicts.

    Shape: [{"type": "text", "text": "<json string>"}] where the JSON has a
    top-level "resources": [{"resource": {...document...}}].
    """
    payload = _unwrap_envelope(envelope)
    return [r.get("resource", r) for r in payload.get("resources", [])]


_POS = ("strong", "beat", "exceed", "surpass", "growth", "raised", "record",
        "positive", "gains", "up ", "wins", "expand", "breakthrough")
_NEG = ("disappoint", "miss", "fell", "decline", "down ", "cut", "trimmed",
        "negative", "risk", "loss", "lawsuit", "discount", "pressure", "bearish")


def _coarse_sentiment(text: str) -> str | None:
    t = text.lower()
    p = sum(t.count(w) for w in _POS)
    n = sum(t.count(w) for w in _NEG)
    if p == 0 and n == 0:
        return None
    if p >= n * 2:
        return "positive"
    if n >= p * 2:
        return "negative"
    return "neutral"


def _source_name(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlparse(url).netloc
        return host[4:] if host.startswith("www.") else host or None
    except Exception:
        return None


def _topic_tag(text: str) -> str | None:
    t = text.lower()
    for tag, kws in (("earnings", ("earnings", "revenue", "eps", "guidance", "quarter")),
                     ("product", ("launch", "product", "agentforce", "feature", "platform")),
                     ("partnership", ("partner", "partnership", "deal", "alliance")),
                     ("market", ("shares", "stock", "investor", "analyst", "valuation"))):
        if any(k in t for k in kws):
            return tag
    return None


def _map_media(resources: list[dict]) -> dict:
    articles, sentiments = [], []
    for r in resources:
        content = r.get("content", "") or ""
        title = r.get("title") or content[:80]
        sent = _coarse_sentiment(f"{title}. {content}")
        sentiments.append(sent)
        articles.append({
            "headline": title,
            "url": r.get("url", ""),
            "source": _source_name(r.get("url")),
            "published_at": r.get("date") or _now(),
            "topic_tag": _topic_tag(f"{title}. {content}"),
            "sentiment": sent,
        })
    pos = sentiments.count("positive")
    neg = sentiments.count("negative")
    summary = None
    if pos or neg:
        summary = "positive" if pos > neg * 1.5 else "negative" if neg > pos * 1.5 else "mixed"
    return {"sentiment": summary, "volume_trend": None,
            "article_count": len(articles), "articles": articles}


def _map_media_search(resp: dict) -> dict:
    """Map the real `mira_search_media_news` shape (pre-aggregated: articles,
    sentiment_summary{positive,neutral,negative}, volume_trend[{date,count}],
    top_topics) into the internal media shape. volume_trend[] is reduced to the
    up/down/flat enum via the existing series helper."""
    mn = resp.get("mira_search_media_news", resp) if isinstance(resp, dict) else {}
    articles = []
    for a in mn.get("articles") or []:
        if not isinstance(a, dict):
            continue
        topics = a.get("topics") or []
        articles.append({
            "headline": a.get("title") or (a.get("summary") or "")[:80],
            "url": a.get("url", ""),
            "source": a.get("source") or _source_name(a.get("url")),
            "published_at": a.get("published_at") or _now(),
            "topic_tag": (topics[0] if topics else None),
            "sentiment": a.get("sentiment") if a.get("sentiment") in ("positive", "negative", "neutral") else None,
        })
    ss = mn.get("sentiment_summary") or {}
    sentiment = None
    if isinstance(ss, dict) and ss:
        pos, neg = ss.get("positive", 0) or 0, ss.get("negative", 0) or 0
        sentiment = "positive" if pos > neg * 1.5 else "negative" if neg > pos * 1.5 else "mixed"
    volume_trend = _volume_trend_from_series(mn.get("volume_trend") or [])
    return {"sentiment": sentiment, "volume_trend": volume_trend,
            "article_count": mn.get("total_articles") or len(articles), "articles": articles}


# --- Statistics enrichment (unified_retrieval statistics_retrieval) -------
# Fills news_feed.volume_trend (rising/falling coverage) and refines
# sentiment_summary using real aggregate counts. Shape-tolerant: the live
# statistics envelope shape is confirmed at wire-up time, so we search a few
# plausible key paths and degrade to None (never fabricate) if absent.

def _find_buckets(payload: Any, key_hints: tuple[str, ...]) -> list[dict]:
    """Best-effort: locate the first list-of-dicts under any container whose key
    matches a hint (e.g. 'time'/'date' for the series, 'sentiment' for breakdown).
    Searches the top level then one level of nested 'aggregations'/'data'/'results'."""
    def _scan(obj: Any) -> list[dict]:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if any(h in k.lower() for h in key_hints) and isinstance(v, list) \
                        and v and isinstance(v[0], dict):
                    return v
            for nest in ("aggregations", "data", "results", "buckets"):
                if isinstance(obj.get(nest), (dict, list)):
                    found = _scan(obj[nest])
                    if found:
                        return found
        elif isinstance(obj, list) and obj and isinstance(obj[0], dict):
            keys = {k.lower() for k in obj[0]}
            if keys & set(key_hints) or any(any(h in k for h in key_hints) for k in keys):
                return obj
        return []
    return _scan(payload)


def _bucket_value(d: dict) -> float:
    for k in ("count", "value", "total", "documents", "docCount", "doc_count"):
        v = d.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


def _volume_trend_from_series(series: list[dict]) -> str | None:
    """Compare the older vs newer half of a temporal count series → up/down/flat."""
    vals = [_bucket_value(b) for b in series]
    if len(vals) < 2 or sum(vals) == 0:
        return None
    mid = len(vals) // 2
    older, newer = sum(vals[:mid]) or 1e-9, sum(vals[mid:])
    ratio = newer / older
    return "up" if ratio >= 1.15 else "down" if ratio <= 0.85 else "flat"


def _sentiment_from_breakdown(buckets: list[dict]) -> str | None:
    """Aggregate a sentiment breakdown (label→count) → positive/negative/mixed."""
    tally: dict[str, float] = {}
    for b in buckets:
        label = str(_first(b, "sentiment", "label", "key", "name", default="")).lower()
        if label in ("positive", "negative", "neutral"):
            tally[label] = tally.get(label, 0.0) + _bucket_value(b)
    pos, neg = tally.get("positive", 0.0), tally.get("negative", 0.0)
    if pos == 0 and neg == 0:
        return None
    return "positive" if pos > neg * 1.5 else "negative" if neg > pos * 1.5 else "mixed"


def _collect_agg_buckets(payload: Any, agg_type: str) -> list[dict]:
    """Walk a Meltwater unified-aggregation envelope and sum bucket counts by key for
    every nested aggregation whose `type` matches `agg_type` ("date" or "sentiment").

    Real shape (confirmed live 2026-06-02):
        resources[].resource.buckets[platform].aggregations[].buckets[]
    where each leaf bucket is {"key": <date|sentiment>, "values": {"count": N}}.
    Series are split per platform, so we aggregate by key across all platforms.
    Returns [{"key": k, "count": total}] sorted by key (dates sort chronologically)."""
    totals: dict[str, float] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == agg_type and isinstance(node.get("buckets"), list):
                for b in node["buckets"]:
                    if not isinstance(b, dict):
                        continue
                    k = b.get("key")
                    if k is None:
                        continue
                    totals[k] = totals.get(k, 0.0) + _bucket_value(b.get("values", b))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(payload)
    return [{"key": k, "count": totals[k]} for k in sorted(totals)]


def _map_statistics(volume_env: Any = None, sentiment_env: Any = None) -> dict:
    """Map statistics_retrieval envelopes → {volume_trend, sentiment}. Either may be
    omitted; missing/unparseable inputs yield None for that field (honest degrade).

    Tries the generic `_find_buckets` heuristic first (covers the simpler sample
    fixtures), then falls back to the confirmed Meltwater unified-aggregation walker
    (`_collect_agg_buckets`) for the real `resources[].resource.buckets[]` shape."""
    out: dict = {"volume_trend": None, "sentiment": None}
    if volume_env is not None:
        try:
            payload = _unwrap_envelope(volume_env)
            series = _find_buckets(payload, ("time", "date", "week", "day", "period"))
            if not series:
                series = _collect_agg_buckets(payload, "date")
            out["volume_trend"] = _volume_trend_from_series(series)
        except Exception:
            pass
    if sentiment_env is not None:
        try:
            payload = _unwrap_envelope(sentiment_env)
            buckets = _find_buckets(payload, ("sentiment",))
            if not buckets or not any(
                str(_first(b, "sentiment", "label", "key", "name", default="")).lower()
                in ("positive", "negative", "neutral") for b in buckets
            ):
                buckets = _collect_agg_buckets(payload, "sentiment")
            out["sentiment"] = _sentiment_from_breakdown(buckets)
        except Exception:
            pass
    return out


# --- Source adapters ------------------------------------------------------
# Each returns structured JSON, tolerant of failure (fetch_all also guards).
#
# Mock policy: Owler / GenAI Lens / Gong connectors are not yet wired. Their mock
# bodies return HARDCODED Salesforce data, which is wrong for any other domain. So
# mocks are gated behind BATTLECARD_ALLOW_MOCK (default OFF): unconnected sources
# return None and the card degrades honestly (lower confidence, "coming soon"
# sections) instead of fabricating competitor facts. Set the flag for demos; mock
# data is then tagged `_mock` so normalize labels it "(simulated)" and excludes it
# from overall_confidence.

def _allow_mock() -> bool:
    return os.environ.get("BATTLECARD_ALLOW_MOCK", "").lower() in ("1", "true", "yes", "on")


# Per-source response mappers. Real connectors return their own shapes; each needs
# a mapper into the internal shape normalize() expects (cf. _map_media). These are
# the explicit swap points — fill in once the tool response shape is known.

def _first(d: dict, *keys, default=None):
    """Return the first present, non-None value among keys (shape tolerance)."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def _map_owler(resp: dict) -> dict:
    """Map an owler_company_details response to the internal overview shape.

    Expected (representative) shape — adjust keys to the live API once available:
        {"company": {"name","domain","description"|"shortDescription",
          "estimatedRevenue"|"revenue","employeeCount"|"numberOfEmployees",
          "headquarters": {"city","state"} | "hq","foundedYear"|"yearFounded",
          "ceo"|"chiefExecutive","totalFunding"|"funding",
          "topCompetitors": [{"name"}|str]}}
    Internal -> {name, description, revenue, headcount, hq, founded, ceo,
                 funding_total, top_competitors:[str]}
    """
    if isinstance(resp, dict) and "owler_company_details" in resp:
        return _map_owler_real(resp)
    c = resp.get("company", resp) if isinstance(resp, dict) else {}
    hq = _first(c, "hq", "headquarters")
    if isinstance(hq, dict):
        hq = ", ".join(x for x in (hq.get("city"), hq.get("state") or hq.get("country")) if x) or None
    comps = _first(c, "topCompetitors", "top_competitors", "competitors", default=[]) or []
    comps = [x.get("name") if isinstance(x, dict) else x for x in comps]
    return {
        "name": _first(c, "name", "companyName"),
        "description": _first(c, "description", "shortDescription", "summary"),
        "revenue": _first(c, "estimatedRevenue", "revenue"),
        "headcount": _first(c, "employeeCount", "numberOfEmployees", "headcount"),
        "hq": hq,
        "founded": _first(c, "foundedYear", "yearFounded", "founded"),
        "ceo": _first(c, "ceo", "chiefExecutive"),
        "funding_total": _first(c, "totalFunding", "funding", "funding_total"),
        "top_competitors": [x for x in comps if x],
    }


def _map_owler_real(resp: dict) -> dict:
    """Map the real Owler tool shape (`owler_company_details` + optional
    `mira_get_company_intelligence`) to the internal overview shape."""
    c = resp.get("owler_company_details", {}) or {}
    hq = c.get("headquarters")
    if isinstance(hq, dict):
        hq = ", ".join(x for x in (hq.get("city"), hq.get("state") or hq.get("country")) if x) or None
    execs = c.get("executives") or []
    ceo = None
    for e in execs:
        if isinstance(e, dict) and "ceo" in (e.get("title") or "").lower():
            ceo = e.get("name"); break
    if not ceo and execs and isinstance(execs[0], dict):
        ceo = execs[0].get("name")
    funding = (c.get("funding") or {}).get("total_raised_usd") if isinstance(c.get("funding"), dict) else None
    comps = [_domain_to_name(x) for x in (c.get("competitors") or [])]
    intel = resp.get("mira_get_company_intelligence") or {}
    desc = c.get("description") or intel.get("intelligence_summary")
    return {
        "name": c.get("name"),
        "description": desc,
        "revenue": _fmt_usd(c.get("revenue_usd")) or c.get("revenue_range"),
        "headcount": c.get("employee_count") if isinstance(c.get("employee_count"), int) else None,
        "hq": hq,
        "founded": c.get("founded_year"),
        "ceo": ceo,
        "funding_total": _fmt_usd(funding),
        "top_competitors": [x for x in comps if x],
    }


_MODEL_ENUM = {"chatgpt", "gemini", "claude", "perplexity", "other"}


def _norm_model(name: str | None) -> str:
    n = (name or "").lower()
    if "gpt" in n or "openai" in n or "chatgpt" in n:
        return "chatgpt"
    if "gemini" in n or "bard" in n or "google" in n:
        return "gemini"
    if "claude" in n or "anthropic" in n:
        return "claude"
    if "perplex" in n:
        return "perplexity"
    return n if n in _MODEL_ENUM else "other"


def _map_genai_lens(resp: dict) -> dict:
    """Map a genai_lens_brandAnalysis response to the internal ai_perception shape.

    Expected (representative) shape — adjust keys to the live API once available:
        {"summary"|"overview", "visibilityScore"|"visibility_score",
         "competitorVisibilityScore"|"vsVisibilityScore",
         "models"|"modelBreakdown": [{"model"|"provider",
            "characterization"|"summary","categoryRank"|"rank_for_category"}],
         "strengthAspects": [str|{"aspect"}], "weaknessAspects": [...]}
    """
    if isinstance(resp, dict) and "genai_lens_brandAnalysis" in resp:
        return _map_genai_lens_real(resp)
    r = resp if isinstance(resp, dict) else {}
    models = _first(r, "models", "modelBreakdown", "model_breakdown", default=[]) or []
    mb = []
    for m in models:
        if not isinstance(m, dict):
            continue
        mb.append({"model": _norm_model(_first(m, "model", "provider")),
                   "characterization": _first(m, "characterization", "summary", "description", default=""),
                   "rank_for_category": _first(m, "categoryRank", "rank_for_category", "rank")})

    def _aspects(key1, key2):
        items = _first(r, key1, key2, default=[]) or []
        return [x.get("aspect") if isinstance(x, dict) else x for x in items]

    return {
        "summary": _first(r, "summary", "overview", default="AI perception coming soon"),
        "visibility_score": _first(r, "visibilityScore", "visibility_score"),
        "vs_visibility_score": _first(r, "competitorVisibilityScore", "vsVisibilityScore", "vs_visibility_score"),
        "model_breakdown": mb,
        "strength_aspects": _aspects("strengthAspects", "strength_aspects"),
        "weakness_aspects": _aspects("weaknessAspects", "weakness_aspects"),
    }


def _map_genai_lens_real(resp: dict) -> dict:
    """Map the real GenAI Lens shape (`genai_lens_brandAnalysis` +
    `genai_lens_entityAspectProfile`) to the internal ai_perception shape."""
    ba = resp.get("genai_lens_brandAnalysis", {}) or {}
    ap = resp.get("genai_lens_entityAspectProfile", {}) or {}
    pv = ba.get("prompt_visibility") or {}
    consensus = ba.get("brand_description_consensus") or ""
    # One model_breakdown row per analyzed model; share the consensus framing and
    # the prompt-visibility rank (the sample reports a single category rank).
    rank = pv.get("rank")
    mb = [{"model": _norm_model(m),
           "characterization": consensus[:200],
           "rank_for_category": rank}
          for m in (ba.get("models_analyzed") or [])]
    aspects = ap.get("aspects") or []
    detail = [{"aspect": a.get("aspect"), "sentiment": a.get("sentiment"),
               "score": a.get("score"),
               "phrases": a.get("representative_phrases") or []}
              for a in aspects if isinstance(a, dict) and a.get("aspect")]
    strengths = [a["aspect"] for a in detail if a["sentiment"] == "positive"]
    weaknesses = [a["aspect"] for a in detail if a["sentiment"] == "negative"]
    return {
        "summary": consensus or "AI perception coming soon",
        "visibility_score": ba.get("overall_visibility_score"),
        "vs_visibility_score": None,
        "model_breakdown": mb,
        "strength_aspects": strengths,
        "weakness_aspects": weaknesses,
        "aspect_detail": detail,
    }


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_URL_RE = re.compile(r"https?://\S+")


def _anonymize(text: str, names: set[str]) -> str:
    """Redact PII from a transcript line before any further processing/storage.
    Org policy: no PII into AI tools. Redacts emails, phone numbers, URLs, and
    known speaker names. (Production: extend with a proper NER pass.)"""
    text = _EMAIL_RE.sub("[EMAIL]", text)
    text = _URL_RE.sub("[URL]", text)
    text = _PHONE_RE.sub("[PHONE]", text)
    for nm in sorted((n for n in names if n and len(n) > 1), key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(nm)}\b", "[NAME]", text, flags=re.I)
    return text


# Objection taxonomy: cue keywords -> canonical, anonymized objection statement.
_OBJECTION_CUES = [
    ("pricing", ("price", "pricing", "expensive", "cost", "costly", "budget", "afford"),
     "Pricing is too high for our budget."),
    ("complexity", ("complex", "complicated", "hard to use", "steep", "learning curve", "overkill"),
     "The platform is too complex for our team size."),
    ("integration", ("integrat", "api", "connect to", "migrat", "existing stack", "interoperab"),
     "Integration with our existing stack is a concern."),
    ("contract", ("contract", "lock-in", "lock in", "commitment", "multi-year", "renewal term"),
     "Contract terms and lock-in are a concern."),
    ("support", ("support", "onboarding", "training", "implementation time", "ramp"),
     "Support and onboarding effort are a concern."),
    ("security", ("security", "compliance", "gdpr", "soc 2", "soc2", "data residency", "privacy"),
     "Security and compliance requirements may not be met."),
    ("roi", ("roi", "return on investment", "value", "justify", "business case"),
     "Hard to justify the ROI to stakeholders."),
]


def _iter_customer_lines(call: dict):
    """Yield text from buyer/customer turns; skip the selling rep."""
    turns = call.get("transcript") or call.get("turns") or []
    if isinstance(turns, str):
        yield turns; return
    for t in turns:
        if not isinstance(t, dict):
            continue
        role = (t.get("speaker_role") or t.get("role") or "").lower()
        if role in ("rep", "seller", "sales", "internal"):
            continue
        txt = t.get("text") or t.get("sentence") or ""
        if txt:
            yield txt


def _map_gong(resp: dict, domain: str | None = None) -> dict:
    """Map a Gong transcript bundle to {objections:[{objection,frequency,call_ref}]}.

    Expected (representative) shape:
        {"calls": [{"id"|"call_ref", "transcript":[{"speaker","speaker_role",
                    "text"}] | "text", "participants":[{"name","role"}]}]}

    Pipeline: anonymize PII -> scan customer turns for objection cues -> aggregate
    by canonical category (frequency across calls, first call_ref) -> top 5.
    Deterministic so it runs with no model; swap in an LLM extractor for nuance.
    """
    if isinstance(resp, dict) and "nx_get_gong_curated_transcript" in resp:
        return _map_gong_real(resp, domain)
    calls = resp.get("calls", resp.get("transcripts", [])) if isinstance(resp, dict) else []
    agg: dict[str, dict] = {}
    for call in calls:
        if not isinstance(call, dict):
            continue
        ref = call.get("call_ref") or call.get("id") or ""
        names = {p.get("name") for p in (call.get("participants") or []) if isinstance(p, dict)}
        seen_in_call = set()  # count a category at most once per call
        for line in _iter_customer_lines(call):
            low = _anonymize(line, names).lower()
            for key, cues, statement in _OBJECTION_CUES:
                if key in seen_in_call:
                    continue
                if any(c in low for c in cues):
                    seen_in_call.add(key)
                    e = agg.setdefault(key, {"objection": statement, "frequency": 0, "call_ref": ref})
                    e["frequency"] += 1
                    if not e["call_ref"]:
                        e["call_ref"] = ref
    objections = sorted(agg.values(), key=lambda x: x["frequency"], reverse=True)[:5]
    return {"objections": objections}


def _map_gong_real(resp: dict, domain: str | None = None) -> dict:
    """Map the real Gong shape: `nx_get_gong_curated_transcript` carries
    pre-extracted objections (objection text, category, suggested_talk_track) per
    call. Aggregate by category across calls (frequency + first call_ref), keep the
    suggested talk track, top 5. PII anonymization still applied to the free text.

    Company-aware: when `domain` is given, keep only objections from calls that
    mention the target competitor (per `nx_list_gong_calls.competitors_mentioned`),
    so the battlecard surfaces objections sellers actually hit against THAT company.
    Falls back to all deal-level objections when the company isn't referenced."""
    transcripts = resp.get("nx_get_gong_curated_transcript") or []
    # call_id -> set(competitor names mentioned), from the call list
    mentions: dict[str, set[str]] = {}
    for c in (resp.get("nx_list_gong_calls") or {}).get("calls") or []:
        if isinstance(c, dict):
            mentions[c.get("call_id")] = {str(x).lower() for x in (c.get("competitors_mentioned") or [])}
    target = domain.split(".")[0].lower() if domain else None

    def _relevant(t):
        if not target:
            return True
        return target in mentions.get(t.get("call_id"), set())

    relevant = [t for t in transcripts if isinstance(t, dict) and _relevant(t)]
    if not relevant:  # company not referenced in calls → fall back to all objections
        relevant = [t for t in transcripts if isinstance(t, dict)]

    agg: dict[str, dict] = {}
    for t in relevant:
        ref = t.get("call_id") or ""
        for ob in t.get("objections") or []:
            if not isinstance(ob, dict):
                continue
            cat = ob.get("category") or ob.get("objection", "")[:32]
            e = agg.get(cat)
            if e is None:
                e = {"objection": _anonymize(ob.get("objection", ""), set()),
                     "frequency": 0, "call_ref": ref}
                tt = ob.get("suggested_talk_track")
                if tt:
                    e["talk_track"] = _anonymize(tt, set())
                agg[cat] = e
            e["frequency"] += 1
    objections = sorted(agg.values(), key=lambda x: x["frequency"], reverse=True)[:5]
    return {"objections": objections}


# --- Demo transports (credible sample data, QA-provided) ------------------
# Each loads a captured real-shape sample and returns the raw sub-response the
# matching mapper expects. Used when mode="demo"; mapped exactly like live data.

def _demo_load(filename: str) -> dict:
    with open(os.path.join(_DEMO_DIR, filename), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _demo_pick_company(data: dict, domain: str) -> dict | None:
    """Select the companies[] entry matching request.domain, or None if absent.
    Returning None (not a fallback company) lets demo mode degrade honestly for a
    domain we have no sample for, instead of mislabeling another company's data."""
    for c in data.get("companies") or []:
        if (c.get("request") or {}).get("domain", "").lower() == domain.lower():
            return c
    return None


def demo_domains() -> set[str]:
    """Domains we have demo sample data for (drives honest degrade for others)."""
    try:
        return {(c.get("request") or {}).get("domain", "").lower()
                for c in _demo_load("owler_sample.json").get("companies") or []}
    except Exception:
        return set()


async def demo_owler_fn(domain: str) -> dict:
    c = _demo_pick_company(_demo_load("owler_sample.json"), domain)
    if c is None:
        raise LookupError(f"no demo Owler sample for {domain}")
    return c


async def demo_genai_fn(domain: str) -> dict:
    c = _demo_pick_company(_demo_load("genai_lens_sample.json"), domain)
    if c is None:
        raise LookupError(f"no demo GenAI Lens sample for {domain}")
    return c


async def demo_gong_fn(domain: str) -> dict:
    # Gong sample is a single curated bundle (Meltwater sales calls vs competitors);
    # only surface it for domains that are part of the demo set.
    if domain.lower() not in demo_domains():
        raise LookupError(f"no demo Gong sample for {domain}")
    return _demo_load("gong_transcripts_sample.json")


async def demo_media_fn(domain: str) -> dict:
    """Returns the internal media dict directly (sample is already aggregated)."""
    c = _demo_pick_company(_demo_load("media_search_sample.json"), domain)
    if c is None:
        raise LookupError(f"no demo Media sample for {domain}")
    return _map_media_search(c)


async def owler_adapter(domain: str, fetch_fn: "SourceFn | None" = None) -> dict | None:
    """Owler company overview. Provide `fetch_fn` (domain -> raw owler response) to
    go live; _map_owler handles the shape. Without it: mock (if allowed) or None."""
    if fetch_fn is not None:
        return _map_owler(await fetch_fn(domain))
    if not _allow_mock():
        return None  # connector not wired
    await asyncio.sleep(0.01)
    return {"_mock": True, "name": "Salesforce", "description": "Cloud-based CRM platform.",
            "revenue": "$31B", "headcount": 73000, "hq": "San Francisco, CA",
            "founded": 1999, "ceo": "Marc Benioff",
            "top_competitors": ["HubSpot", "Microsoft Dynamics", "Oracle"]}


async def media_adapter(domain: str, retrieval_fn: RetrievalFn | None = None,
                        stats_fn: "RetrievalFn | None" = None,
                        sentiment_fn: "RetrievalFn | None" = None,
                        media_fn: "SourceFn | None" = None) -> dict | None:
    """REAL: maps a live unified_retrieval response into news_feed inputs.

    `retrieval_fn` is the document transport (default: captured live fixture).
    Optional `stats_fn`/`sentiment_fn` (statistics_retrieval transports) enrich the
    result with a real volume_trend and aggregate sentiment_summary; when present
    they override the article-level heuristics. On failure, falls back to mock only
    if BATTLECARD_ALLOW_MOCK is set; otherwise returns None (honest degrade).
    """
    if media_fn is not None:
        # Pre-mapped internal media dict (e.g. demo sample, already aggregated).
        try:
            return await media_fn(domain)
        except Exception:
            return None
    fn = retrieval_fn or _fixture_retrieval
    try:
        envelope = await fn(domain)
        resources = _parse_retrieval_envelope(envelope)
        if not resources:
            raise ValueError("no resources in retrieval response")
        media = _map_media(resources)
        if stats_fn is not None or sentiment_fn is not None:
            vol_env = await stats_fn(domain) if stats_fn is not None else None
            sent_env = await sentiment_fn(domain) if sentiment_fn is not None else None
            try:
                stats = _map_statistics(vol_env, sent_env)
                if stats.get("volume_trend") is not None:
                    media["volume_trend"] = stats["volume_trend"]
                if stats.get("sentiment") is not None:
                    media["sentiment"] = stats["sentiment"]
            except Exception:
                pass  # enrichment is best-effort; base media already valid
        return media
    except Exception:
        if not _allow_mock():
            return None
        return {"_mock": True, "sentiment": "mixed", "volume_trend": "up", "article_count": 1,
                "articles": [{"headline": "Sample headline (simulated)",
                              "url": "https://example.com/a", "source": "example.com",
                              "published_at": "2026-05-28T14:00:00Z",
                              "topic_tag": "product", "sentiment": "positive"}]}


async def genai_lens_adapter(domain: str, fetch_fn: "SourceFn | None" = None) -> dict | None:
    """GenAI Lens brand analysis. Provide `fetch_fn` (domain -> raw brandAnalysis
    response) to go live; _map_genai_lens handles the shape."""
    if fetch_fn is not None:
        return _map_genai_lens(await fetch_fn(domain))
    if not _allow_mock():
        return None
    await asyncio.sleep(0.01)
    return {"_mock": True,
            "summary": "LLMs rank it #1 for enterprise CRM but flag cost/complexity.",
            "visibility_score": 91, "vs_visibility_score": 58,
            "model_breakdown": [{"model": "chatgpt",
                                 "characterization": "Market leader, costly.",
                                 "rank_for_category": 1}],
            "strength_aspects": ["market position"], "weakness_aspects": ["ease of use"]}


async def gong_adapter(domain: str, fetch_fn: "SourceFn | None" = None) -> dict | None:
    """Gong objections. Provide `fetch_fn` (domain -> raw transcript bundle) to go
    live; _map_gong does objection extraction (NLP + PII anonymization — TODO)."""
    if fetch_fn is not None:
        return _map_gong(await fetch_fn(domain), domain)
    if not _allow_mock():
        return None
    await asyncio.sleep(0.01)
    return {"_mock": True,
            "objections": [{"objection": "Their platform is too complex for our team size.",
                            "frequency": 14, "call_ref": "call_8842"}]}


# --- Aggregation ----------------------------------------------------------
async def fetch_all(domain: str, retrieval_fn: RetrievalFn | None = None,
                    owler_fn: SourceFn | None = None,
                    genai_fn: SourceFn | None = None,
                    gong_fn: SourceFn | None = None,
                    stats_fn: RetrievalFn | None = None,
                    sentiment_fn: RetrievalFn | None = None,
                    media_fn: SourceFn | None = None) -> dict:
    """Fire all four adapters concurrently; tolerate individual failures.

    Pass a transport per source to go live; omitted sources stay mock/None.
    `stats_fn`/`sentiment_fn` optionally enrich Media with volume_trend + sentiment.
    `media_fn` supplies a pre-mapped media dict (demo mode).
    """
    names = ["owler", "media", "genai_lens", "gong"]
    coros = [owler_adapter(domain, owler_fn),
             media_adapter(domain, retrieval_fn, stats_fn, sentiment_fn, media_fn),
             genai_lens_adapter(domain, genai_fn), gong_adapter(domain, gong_fn)]
    results = await asyncio.gather(*coros, return_exceptions=True)
    return {n: (None if isinstance(r, Exception) else r) for n, r in zip(names, results)}


def normalize(domain: str, raw: dict, vs: str | None = None,
              llm_fn: LlmFn | None = None) -> dict:
    """Map raw source responses into the unified battlecard schema, running the
    LLM synthesis chain (with confidence guardrail) for the claim sections."""
    o, m, g, gong = raw["owler"], raw["media"], raw["genai_lens"], raw["gong"]
    used = [k for k, v in raw.items() if v]
    # Sources whose data is simulated (mock). Excluded from overall_confidence and
    # marked "(simulated)" in attribution labels so the card never overstates trust.
    sim = {k for k, v in raw.items() if v and v.get("_mock")}
    # Demo/sample-backed sources: credible data, counted toward confidence, but
    # labeled "(sample data)" so the card never implies a live connector.
    sample = {k for k, v in raw.items() if v and v.get("_demo")}
    real_used = [k for k in used if k not in sim]

    def _lbl(src, text):
        if src in sim:
            return f"{text} (simulated)"
        if src in sample:
            return f"{text} (sample data)"
        return text

    overview = {"description": (o or {}).get("description", "Unknown"),
                "revenue": (o or {}).get("revenue"), "headcount": (o or {}).get("headcount"),
                "hq": (o or {}).get("hq"), "founded": (o or {}).get("founded"),
                "ceo": (o or {}).get("ceo"),
                "top_competitors": (o or {}).get("top_competitors", []),
                "attribution": ([{"source": "owler", "label": _lbl("owler", "Owler company graph")}]
                                if o else [])}

    strengths = _synth_with_guardrail("strengths", {"media": m, "genai_lens": g}, llm_fn)
    weaknesses = _synth_with_guardrail("weaknesses", {"media": m, "genai_lens": g}, llm_fn)
    objections = _synth_with_guardrail("objections", {"gong": gong}, llm_fn)

    ai_perception = {"summary": (g or {}).get("summary", "AI perception coming soon"),
                     "section_confidence": 0.8 if g else 0.0,
                     "visibility_score": (g or {}).get("visibility_score"),
                     "vs_visibility_score": (g or {}).get("vs_visibility_score"),
                     "model_breakdown": (g or {}).get("model_breakdown", []),
                     "attribution": ([{"source": "genai_lens",
                                       "label": _lbl("genai_lens", "brand analysis")}] if g else [])}
    news_feed = {"section_confidence": 0.85 if m else 0.0,
                 "sentiment_summary": (m or {}).get("sentiment"),
                 "volume_trend": (m or {}).get("volume_trend"),
                 "items": (m or {}).get("articles", [])}

    meta = {"schema_version": "1.0.0",
            "battlecard_id": f"{domain}-{dt.date.today().isoformat()}",
            "company_domain": domain, "company_name": (o or {}).get("name", domain),
            "generated_at": _now(), "cache_ttl_seconds": CACHE_TTL,
            "data_sources_used": used,
            "overall_confidence": round(len(real_used) / 4 * 0.85, 2)}
    if vs:  # schema: vs_company is an optional string, not nullable
        meta["vs_company"] = vs

    card = {
        "meta": meta,
        "company_overview": overview,
        "sections": {"strengths": strengths, "weaknesses": weaknesses,
                     "objections": objections, "ai_perception": ai_perception,
                     "news_feed": news_feed},
    }
    _mark_simulated(card, sim, sample)
    return card


def degraded_card(domain: str, vs: str | None = None, reason: str | None = None) -> dict:
    """A guaranteed schema-valid, content-empty card. Built via `normalize` with all
    sources absent (no synthesis output) so it is valid by construction — used as the
    API's graceful fallback when a freshly generated card fails schema validation."""
    empty = {"owler": None, "media": None, "genai_lens": None, "gong": None}
    card = normalize(domain, empty, vs)
    card["meta"]["degraded"] = True
    card["meta"]["degraded_reason"] = reason or "card unavailable; returning empty card"
    return card


def _mark_simulated(card: dict, sim: set[str], sample: set[str] | None = None) -> None:
    """Append a provenance tag to every attribution label by source: '(simulated)'
    for mock sources, '(sample data)' for demo/sample-backed sources. Covers the
    synth-section attributions that aren't built via normalize's _lbl."""
    sample = sample or set()
    if not sim and not sample:
        return

    def tag_for(src):
        if src in sim:
            return "(simulated)"
        if src in sample:
            return "(sample data)"
        return None

    def walk(node):
        if isinstance(node, dict):
            for a in node.get("attribution", []) or []:
                if isinstance(a, dict):
                    tag = tag_for(a.get("source"))
                    lbl = a.get("label", "")
                    if tag and tag not in lbl:
                        a["label"] = (lbl + " " + tag).strip()
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(card)


# --- LLM synthesis chain --------------------------------------------------
# Real prompt-per-section templates. Source data is embedded both as natural
# language (for a real model) and in a machine-readable <<DATA>>...<<END>> block
# (so the default deterministic llm_fn can synthesize without a real model).

_PROMPTS = {
    "strengths": (
        "You are a competitive intelligence analyst. Using only the evidence "
        "below, list this competitor's top STRENGTHS as a sales battlecard would. "
        "Return a JSON object {\"items\":[{\"claim\",\"evidence\",\"confidence\","
        "\"attribution\":[{\"source\",\"label\"}]}]}. Every claim must cite a source; "
        "set confidence 0-1 by evidence strength. SECTION=strengths\n<<DATA>>__DATA__<<END>>"),
    "weaknesses": (
        "You are a competitive intelligence analyst. Using only the evidence "
        "below, list this competitor's top WEAKNESSES a seller could exploit. "
        "Return JSON {\"items\":[{\"claim\",\"evidence\",\"confidence\","
        "\"attribution\":[{\"source\",\"label\"}]}]}. Cite sources; score confidence "
        "0-1. SECTION=weaknesses\n<<DATA>>__DATA__<<END>>"),
    "objections": (
        "You are a sales enablement coach. For each customer OBJECTION from Gong "
        "calls below, write a concise counter talk_track. Return JSON {\"items\":"
        "[{\"objection\",\"talk_track\",\"frequency\",\"confidence\","
        "\"attribution\":[{\"source\",\"ref\",\"label\"}]}]}, max 5 items. "
        "SECTION=objections\n<<DATA>>__DATA__<<END>>"),
}


_REQUIRED_KEYS = {  # mirrors battlecard.schema.json item-level `required`
    "strengths": {"claim", "confidence"},
    "weaknesses": {"claim", "confidence"},
    "objections": {"objection", "talk_track", "confidence"},
}


# Prompt-injection guard. Source text (esp. media article headlines/snippets) is
# attacker-influenceable and flows into the LLM prompt's <<DATA>> block. A crafted
# string could (a) forge the <<DATA>>/<<END>> delimiters or SECTION= tag to break out
# of the data block / spoof a section, or (b) carry imperative instructions a real
# model might follow. We neutralize both at the single chokepoint (_build_prompt).
_PROMPT_TOKEN_RE = re.compile(r"<<\s*/?\s*(?:DATA|END)\s*>>|SECTION\s*=", re.I)
_INJECTION_RE = re.compile(
    r"(?i)\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}"
    r"\b(?:previous|prior|above|earlier|all)\b[^.\n]{0,20}"
    r"\b(?:instruction|prompt|direction|rule|context)s?\b"
    r"|(?i)\b(?:system|developer)\s+prompt\b"
    r"|(?i)\byou\s+are\s+now\b"
    r"|(?i)\bnew\s+(?:instruction|rule|task)s?\b")
_MAX_FIELD_LEN = 2000


def _sanitize_for_prompt(node):
    """Recursively neutralize untrusted text before it enters the LLM prompt.
    Strips prompt delimiters/SECTION tokens, redacts injection imperatives, and caps
    length. Keys are left intact; only string *values* are scrubbed."""
    if isinstance(node, str):
        s = _PROMPT_TOKEN_RE.sub(" ", node)
        s = _INJECTION_RE.sub("[redacted]", s)
        if len(s) > _MAX_FIELD_LEN:
            s = s[:_MAX_FIELD_LEN] + "…"
        return s
    if isinstance(node, dict):
        return {k: _sanitize_for_prompt(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_sanitize_for_prompt(v) for v in node]
    return node


def _build_prompt(section: str, inputs: dict) -> str:
    safe = _sanitize_for_prompt(inputs)
    return _PROMPTS[section].replace("__DATA__", json.dumps(safe, default=str))


def _synth_claims(section: str, data: dict) -> list[dict]:
    """Build company-specific strength/weakness claims from the actual source data:
    GenAI Lens aspect scores/phrases + media topics. Deterministic stand-in for the
    model; degrades to a generic claim only when no structured signal is present."""
    m, g = data.get("media") or {}, data.get("genai_lens") or {}
    if not (m or g):
        return []
    want_pos = section == "strengths"
    items: list[dict] = []

    # 1) GenAI Lens aspects (scored, with representative phrases) — the strongest signal.
    # Strengths = positive aspects + high-scoring neutrals; weaknesses = negative
    # aspects + low-scoring neutrals (so a brand with few hard negatives still shows
    # its softer gaps, e.g. enterprise scalability).
    detail = g.get("aspect_detail") or []

    def _matches(d):
        sent, score = d.get("sentiment"), d.get("score") or 50
        if want_pos:
            return sent == "positive" or (sent == "neutral" and score >= 70)
        return sent == "negative" or (sent == "neutral" and score <= 55)

    picked = [d for d in detail if _matches(d)]
    picked.sort(key=lambda d: d.get("score") or 0, reverse=want_pos)  # best strengths / worst weaknesses first
    for d in picked[:3]:
        score = d.get("score")
        phrases = ", ".join((d.get("phrases") or [])[:3]) or None
        verb = "Strong" if want_pos else "Weak on"
        items.append({
            "claim": f"{verb} {str(d.get('aspect')).lower()}.",
            "evidence": (f"AI models cite: {phrases}." if phrases else None),
            "confidence": round(min(0.95, 0.5 + abs((score or 50) - 50) / 100), 2),
            "attribution": [{"source": "genai_lens",
                             "label": f"aspect: {d.get('aspect')}"
                                      + (f" ({score}/100)" if score is not None else "")}],
        })

    # 2) Media topic signal — reinforce with a positive/negative coverage theme.
    if m.get("articles"):
        want_sent = "positive" if want_pos else "negative"
        topics = []
        for a in m["articles"]:
            t = a.get("topic_tag")
            if t and a.get("sentiment") == want_sent and t not in topics:
                topics.append(t)
        if topics:
            items.append({
                "claim": ("Favorable" if want_pos else "Unfavorable")
                         + f" press momentum around {topics[0]}.",
                "evidence": f"Recent coverage trends {want_sent} on: {', '.join(topics[:3])}.",
                "confidence": 0.7,
                "attribution": [{"source": "media",
                                 "label": f"{m.get('article_count', '?')} articles"}],
            })

    # 3) Fallback so the section never renders empty when a source did return.
    if not items:
        items.append({
            "claim": ("Established market presence." if want_pos
                      else "Faces competitive pressure on price and complexity."),
            "confidence": 0.55,
            "attribution": [{"source": "genai_lens" if g else "media", "label": "overall signal"}],
        })
    return items[:4]


def _default_llm_fn(prompt: str) -> str:
    """Deterministic stand-in for a real model: reads the SECTION tag and the
    embedded <<DATA>> block and synthesizes a schema-shaped JSON string. A real
    model would instead read the natural-language instructions. Swap via llm_fn."""
    section = re.search(r"SECTION=(\w+)", prompt)
    section = section.group(1) if section else ""
    data = {}
    blk = re.search(r"<<DATA>>(.*)<<END>>", prompt, re.S)
    if blk:
        try:
            data = json.loads(blk.group(1))
        except Exception:
            data = {}

    if section in ("strengths", "weaknesses"):
        return json.dumps({"items": _synth_claims(section, data)})

    if section == "objections":
        gong = data.get("gong") or {}
        items = []
        for ob in gong.get("objections", [])[:5]:
            items.append({
                "objection": ob["objection"],
                "talk_track": ob.get("talk_track") or "Position faster time-to-value and lower admin overhead; offer a guided onboarding comparison.",
                "frequency": ob.get("frequency"),
                "confidence": 0.83,
                "attribution": [{"source": "gong", "ref": ob.get("call_ref", ""),
                                 "label": f"{ob.get('frequency', '?')} calls"}]})
        return json.dumps({"items": items})

    return json.dumps({"items": []})


_LLM_MODEL = os.environ.get("BATTLECARD_LLM_MODEL", "claude-sonnet-4-6")
_LLM_SYSTEM = ("You are a competitive-intelligence analyst. Respond with ONLY a "
               "single JSON object matching the requested shape — no prose, no "
               "code fences. Ground every claim in the supplied evidence and never "
               "invent sources.")


def anthropic_llm_fn(prompt: str) -> str:
    """Real model transport via the Anthropic Messages API. Requires the
    `anthropic` package and ANTHROPIC_API_KEY in the environment."""
    import anthropic  # lazy: only needed when this transport is selected
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    msg = client.messages.create(
        model=_LLM_MODEL, max_tokens=1024, system=_LLM_SYSTEM,
        messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")


def _resolve_llm_fn(llm_fn: LlmFn | None) -> LlmFn:
    """Explicit override > Anthropic (if key + package present) > deterministic stub."""
    if llm_fn is not None:
        return llm_fn
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic  # noqa: F401
            return anthropic_llm_fn
        except Exception:
            pass
    return _default_llm_fn


def _resolve_statistics_fns(
    stats_fn: "RetrievalFn | None",
    sentiment_fn: "RetrievalFn | None",
) -> "tuple[RetrievalFn | None, RetrievalFn | None]":
    """Resolve the Media statistics transports for live mode.

    Explicit override wins. Otherwise build real transports (volume + sentiment) when the
    env has MELTWATER_MCP_URL plus credentials. An MCP endpoint (URL containing "/mcp")
    uses the JSON-RPC `make_mcp_tool_fn` and authenticates on MELTWATER_MCP_API_KEY alone
    (the APIM subscription key — confirmed sufficient live; the Bearer JWT is optional).
    A non-MCP URL uses the plain-POST `make_http_statistics_fn` and needs the JWT. With no
    creds, returns (None, None) so the card degrades honestly (no fabrication).

        export MELTWATER_MCP_URL=... MELTWATER_MCP_API_KEY=...   # MCP: api-key is enough
        # optional: Bearer JWT (only if a future endpoint requires user-scoped auth)
        export MELTWATER_MCP_JWT=...
        # optional: override the tool name if it differs in prod
        export MELTWATER_MCP_STATS_TOOL=unified_retrieval_statistics_retrieval_tool
    """
    url = os.environ.get("MELTWATER_MCP_URL")
    jwt = os.environ.get("MELTWATER_MCP_JWT")
    api_key = os.environ.get("MELTWATER_MCP_API_KEY")
    tool = os.environ.get("MELTWATER_MCP_STATS_TOOL", MELTWATER_STATS_TOOL)
    is_mcp = bool(url) and "/mcp" in url
    # MCP authenticates on api-key alone; non-MCP HTTP needs the JWT.
    have_creds = bool(url) and ((api_key or jwt) if is_mcp else bool(jwt))

    def _build(query_builder):
        if is_mcp:
            return make_mcp_tool_fn(url, jwt, api_key, tool_name=tool,
                                    arg_builder=query_builder)
        return make_http_statistics_fn(url, jwt, api_key, query_builder=query_builder)

    if stats_fn is None and have_creds:
        stats_fn = _build(build_volume_query)
    if sentiment_fn is None and have_creds:
        sentiment_fn = _build(build_sentiment_query)
    return stats_fn, sentiment_fn


def _resolve_retrieval_fn(retrieval_fn: "RetrievalFn | None") -> "RetrievalFn | None":
    """Resolve the Media DOCUMENT transport (news articles) for live mode.

    Explicit override wins. Otherwise, if MELTWATER_MCP_URL is an MCP endpoint with an
    api-key (JWT optional), build the document-retrieval transport via `make_mcp_tool_fn`
    (tool name MELTWATER_DOC_TOOL, args from `build_media_query`). With no creds, returns
    None — `media_adapter` then falls back to its captured fixture (unchanged behavior).
    Response shape ([{type,text}] -> resources[].resource) is what `_parse_retrieval_
    envelope` already handles (confirmed live 2026-06-02)."""
    if retrieval_fn is not None:
        return retrieval_fn
    url = os.environ.get("MELTWATER_MCP_URL")
    jwt = os.environ.get("MELTWATER_MCP_JWT")
    api_key = os.environ.get("MELTWATER_MCP_API_KEY")
    tool = os.environ.get("MELTWATER_MCP_DOC_TOOL", MELTWATER_DOC_TOOL)
    if url and "/mcp" in url and (api_key or jwt):
        return make_mcp_tool_fn(url, jwt, api_key, tool_name=tool, arg_builder=build_media_query)
    return None


# Per-source env-var prefixes for the live transports. Each is independent: a source
# goes live only when ITS url+token are set; otherwise it stays None and the card
# degrades honestly for that source (no mock, no fabrication).
_SOURCE_ENV = {
    "owler": ("OWLER_MCP_URL", "OWLER_MCP_JWT", "OWLER_MCP_API_KEY"),
    "genai": ("GENAI_LENS_MCP_URL", "GENAI_LENS_MCP_JWT", "GENAI_LENS_MCP_API_KEY"),
    "gong": ("GONG_API_URL", "GONG_API_TOKEN", "GONG_API_KEY"),
}


def _resolve_source_fns(
    owler_fn: "SourceFn | None",
    genai_fn: "SourceFn | None",
    gong_fn: "SourceFn | None",
) -> "tuple[SourceFn | None, SourceFn | None, SourceFn | None]":
    """Resolve Owler / GenAI Lens / Gong transports for live mode.

    Explicit override wins per source. Otherwise, for each source whose
    <PREFIX>_URL + <PREFIX>_(JWT|TOKEN) are in the environment, build an HTTP transport
    via `make_http_source_fn`. Sources without creds stay None (honest degrade).

        export OWLER_MCP_URL=... OWLER_MCP_JWT=...
        export GENAI_LENS_MCP_URL=... GENAI_LENS_MCP_JWT=...
        export GONG_API_URL=... GONG_API_TOKEN=...
    """
    resolved = {"owler": owler_fn, "genai": genai_fn, "gong": gong_fn}
    for name, (url_k, tok_k, key_k) in _SOURCE_ENV.items():
        if resolved[name] is not None:
            continue  # explicit override
        url, tok, api_key = (os.environ.get(url_k), os.environ.get(tok_k),
                             os.environ.get(key_k))
        if url and tok:
            resolved[name] = make_http_source_fn(url, tok, api_key)
    return resolved["owler"], resolved["genai"], resolved["gong"]


def _extract_json(text: str) -> dict:
    """Parse a model completion that may be wrapped in prose or ```json fences."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)  # first/outermost object
        if m:
            return json.loads(m.group(0))
        raise


def _score_section(section: str, items: list[dict], inputs: dict) -> float:
    """Confidence = coverage (did sources return?) blended with mean item confidence."""
    have = any(v for v in inputs.values())
    if not items or not have:
        return 0.0
    mean_item = sum(i.get("confidence", 0.0) for i in items) / len(items)
    return round(min(1.0, 0.5 * mean_item + 0.5 * (1.0 if have else 0.0)), 2)


def _synth_section(section: str, inputs: dict, llm_fn: LlmFn) -> dict:
    prompt = _build_prompt(section, inputs)
    try:
        items = _extract_json(llm_fn(prompt)).get("items", [])
    except Exception:
        # transport/parse failure → deterministic stub so the section still renders
        try:
            items = _extract_json(_default_llm_fn(prompt)).get("items", [])
        except Exception:
            items = []
    # Drop malformed items so a misbehaving model degrades gracefully instead of
    # producing schema-invalid output downstream.
    req = _REQUIRED_KEYS[section]
    items = [i for i in items if isinstance(i, dict) and req.issubset(i)]
    return {"items": items[:5] if section == "objections" else items,
            "section_confidence": _score_section(section, items, inputs)}


def _synth_with_guardrail(section: str, inputs: dict, llm_fn: LlmFn | None) -> dict:
    """Synthesize a section; if confidence falls below the floor, regenerate once."""
    fn = _resolve_llm_fn(llm_fn)
    out = _synth_section(section, inputs, fn)
    if out["section_confidence"] < CONFIDENCE_FLOOR and any(inputs.values()):
        retry = _synth_section(section, inputs, fn)
        if retry["section_confidence"] >= out["section_confidence"]:
            out = retry
    return out


# --- Public entrypoint -----------------------------------------------------
async def generate_battlecard(domain: str, vs: str | None = None, fresh: bool = False,
                              retrieval_fn: RetrievalFn | None = None,
                              owler_fn: SourceFn | None = None,
                              genai_fn: SourceFn | None = None,
                              gong_fn: SourceFn | None = None,
                              stats_fn: RetrievalFn | None = None,
                              sentiment_fn: RetrievalFn | None = None,
                              media_fn: SourceFn | None = None,
                              llm_fn: LlmFn | None = None,
                              mode: str = "live") -> dict:
    """`mode="live"` (default): use injected/connector transports, honest degrade.
    `mode="demo"`: drive all four sources from QA sample data (mapped exactly like
    live), tagged `_demo` so the card is realistic and labeled "(sample data)".
    Explicitly-passed transports override the demo defaults per source."""
    demo = mode == "demo"
    key = f"{domain}|{vs}|{mode}"
    cache = get_cache()
    if not fresh:
        cached = cache.get(key)
        if cached is not None:
            return cached
    if demo:
        owler_fn = owler_fn or demo_owler_fn
        genai_fn = genai_fn or demo_genai_fn
        gong_fn = gong_fn or demo_gong_fn
        media_fn = media_fn or demo_media_fn
    else:  # live: auto-wire transports from env creds (each source independent; else None)
        retrieval_fn = _resolve_retrieval_fn(retrieval_fn)
        stats_fn, sentiment_fn = _resolve_statistics_fns(stats_fn, sentiment_fn)
        owler_fn, genai_fn, gong_fn = _resolve_source_fns(owler_fn, genai_fn, gong_fn)
    raw = await fetch_all(domain, retrieval_fn, owler_fn, genai_fn, gong_fn,
                          stats_fn, sentiment_fn, media_fn)
    if demo:  # tag sample-backed sources so attribution reads "(sample data)"
        for v in raw.values():
            if isinstance(v, dict):
                v["_demo"] = True
    card = normalize(domain, raw, vs, llm_fn)
    cache.set(key, card)
    return card


if __name__ == "__main__":
    import sys
    _mode = sys.argv[1] if len(sys.argv) > 1 else "live"
    card = asyncio.run(generate_battlecard("salesforce.com", vs="HubSpot", mode=_mode))
    print(json.dumps(card, indent=2))
