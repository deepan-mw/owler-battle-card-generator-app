# Owler Battlecard Engine

A living competitive battlecard: enter a competitor domain, fetch four Meltwater
data sources in parallel, synthesize with an LLM, and return a schema-valid
battlecard JSON. Hackathon XXXV — team "Owler Battlecard Engine" (Deepan: Developer; Arun: Data & QA).

## Files

- `battlecard.schema.json` — output contract (JSON Schema Draft 2020-12). Six
  sections plus meta, with per-claim source attribution and 0–1 confidence scores.
- `battlecard.example.json` — a worked, valid instance (Salesforce vs. HubSpot).
- `aggregator.py` — working skeleton. Runs today on mock data and produces
  schema-valid output.

## Architecture

`domain → fetch_all (4 sources, concurrent) → normalize → _synth_* (LLM) → battlecard JSON`, with a 24h in-memory cache.

| Source | Adapter | Real tool to swap in |
|---|---|---|
| Owler | `owler_adapter` | `owler_company_details` / `mira_get_company_intelligence` |
| Media | `media_adapter` | `mira_search_media_news` / `unified_retrieval` |
| GenAI Lens | `genai_lens_adapter` | `genai_lens_brandAnalysis` / `entityAspectProfile` / `promptVisibilityGaps` |
| Gong | `gong_adapter` | `nx_list_gong_calls` / `nx_get_gong_curated_transcript` |

## Run

```bash
pip3 install -r requirements.txt

python3 aggregator.py                 # prints a battlecard for salesforce.com
uvicorn api:app --reload              # GET http://localhost:8000/battlecard?domain=salesforce.com&vs=HubSpot
open frontend/index.html              # UI; talks to the API, falls back to the bundled sample
```

Validate any output:

```python
import asyncio, json, jsonschema, aggregator
c = asyncio.run(aggregator.generate_battlecard("salesforce.com", vs="HubSpot"))
jsonschema.validate(c, json.load(open("battlecard.schema.json")))
```

## Demo vs Live mode

The engine runs in two modes, selectable per request — `generate_battlecard(..., mode="demo"|"live")`, the API's `?mode=`, or the **Demo/Live toggle** in the UI header.

- **Live** (default) — uses connector/injected transports; unconnected sources
  degrade honestly (see *Mock policy* below). For a domain with no live sources,
  the card is sparse and low-confidence.
- **Demo** — drives all four sources from credible, real-shaped sample data
  (`fixtures/demo/`, provided by QA), mapped through the *same* `_map_*` mappers
  as live. Produces a full four-source card (`overall_confidence` ~0.85), but
  every attribution is labeled **"(sample data)"** so it's never mistaken for live
  output. Because demo and live share the mappers, wiring a real connector later
  is a no-op for the demo path. Sample data is available for **salesforce.com,
  hubspot.com, microsoft.com, and klue.com**; any other domain degrades honestly
  in demo mode (no source borrows another company's data), exactly like live.

```bash
# API
curl "http://localhost:8000/battlecard?domain=salesforce.com&vs=HubSpot&mode=demo"
curl "http://localhost:8000/battlecard?domain=salesforce.com&mode=live"   # default
# bad mode → 422

# Python
card = await generate_battlecard("salesforce.com", vs="HubSpot", mode="demo")
```

Demo transports (`demo_owler_fn` / `demo_genai_fn` / `demo_gong_fn` / `demo_media_fn`)
load `fixtures/demo/*_sample.json`; an explicitly-passed transport overrides the
demo default per source. Demo data is tagged `_demo` → counted toward confidence
but labeled "(sample data)", distinct from `_mock` "(simulated)".

## Status (build of 2026-06-01)

| Task | State |
|---|---|
| Media adapter → real call | **Done.** Maps live `unified_retrieval` response → `news_feed`. Transport is the injectable `retrieval_fn` (default replays a captured live fixture; prod = HTTP call). Source/sentiment/topic derived per article. |
| Owler / GenAI Lens / Gong | Mock, refactored to real-or-fallback. Connectors not yet available — wiring each is a one-line swap at the `# TODO real:` lines. |
| LLM synthesis chain | **Done.** Prompt-per-section + Gong objection extraction + confidence scoring + regenerate-below-0.5 guardrail. Model call is the injectable `llm_fn` (default deterministic; swap for Anthropic/gateway). |
| FastAPI endpoint | **Done.** `api.py` — `GET /battlecard?domain=&vs=&fresh=`, validates every response against the schema, 422 on bad domain. |
| Frontend | **Done.** `frontend/index.html` renders all 6 sections with attribution chips + confidence bars. |

### Mock policy (honest-by-default)
Owler / GenAI Lens / Gong connectors aren't wired, and their mock bodies are
hardcoded Salesforce data — wrong for any other domain. So mocks are gated:

- **Default (`BATTLECARD_ALLOW_MOCK` unset):** unconnected sources return `None`.
  The card degrades honestly — those sections show "coming soon"/empty,
  `data_sources_used` lists only what's real, and `overall_confidence` drops
  accordingly. A `hubspot.com` card no longer reports Salesforce's CEO.
- **Demo (`BATTLECARD_ALLOW_MOCK=1`):** mocks fill the layout but every
  mock-sourced claim is labeled `(simulated)` in its attribution, and
  `overall_confidence` still counts only real sources, so the card never
  overstates trust.

Wiring a real source = pass a transport (`owler_fn` / `genai_fn` / `gong_fn` /
`retrieval_fn`) into `generate_battlecard`; each adapter routes it through its
`_map_*` mapper. No transport → mock-gated/None. Example:

```python
async def owler_fn(domain):
    return await owler_company_details(domain)   # or HTTP call
card = await generate_battlecard("salesforce.com", owler_fn=owler_fn)
```

`_map_owler`, `_map_genai_lens`, and `_map_gong` are all implemented (tested
against `fixtures/sample_*_response.json`, which document the expected real shapes —
adjust key names to the live API). Media has a production transport helper,
`make_http_retrieval_fn(base_url, token)`, and `build_media_query`.

**Gong / PII:** `_map_gong` anonymizes each transcript line (`_anonymize`: emails,
phones, URLs, participant names) *before* processing, per the no-PII policy, then
extracts objections via a category taxonomy (`_OBJECTION_CUES`), aggregating
frequency across calls and keeping a `call_ref`. It's deterministic so it runs with
no model; swap in an LLM extractor for nuance. Note the default `_anonymize` is
regex+known-names; production should add a proper NER pass.

Tests: `python3 tests/test_mappers.py`.

### Injection seams (why the service runs with no creds)
`generate_battlecard(domain, vs, fresh, retrieval_fn=, llm_fn=)` — both transports
are injectable. The Python process can't call the session-bound MCP tools directly,
so `retrieval_fn` (Media HTTP API) and `llm_fn` (model API) are the production swap
points; defaults keep everything runnable and schema-valid today.

## Remaining
- Connect Owler / GenAI Lens / Gong MCPs, then swap the three `# TODO real:` adapter bodies.
- Point `retrieval_fn` and `llm_fn` at the real HTTP/model endpoints for production.

Keep every output schema-valid. Test company: `salesforce.com`.

## Risks / fallbacks

- Gong access denied → demo with pre-extracted cached transcripts; flag section "coming soon".
- LLM hallucination → source attribution on every claim + confidence gate.
- No GenAI Lens coverage → graceful "AI perception coming soon".
