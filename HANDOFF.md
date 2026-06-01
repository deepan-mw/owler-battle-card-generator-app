# Battle Cards Generator — Handoff (as of 2026-06-02, eod)

> Product renamed in the UI to "Battle Cards Generator" (engine internals/files
> still use the battlecard naming). Read `workflow_state.md` Log (2026-06-02a→h)
> + global CLAUDE.md before continuing.

> Folder note: project root was renamed from `project/` to `battle-card-generator-app/`.
> All paths below are relative to that folder.

## What it is
`domain → fetch_all (4 sources, concurrent) → normalize → LLM synthesis → schema-valid
battlecard JSON`, 24h cache. Output conforms to `battlecard.schema.json`. Test company:
`salesforce.com`. Workflow tracked in `workflow_state.md` (read it + global CLAUDE.md first).

## Files
- `aggregator.py` — core engine (adapters, mappers, LLM chain, cache).
- `api.py` — FastAPI: `GET /battlecard?domain=&vs=&fresh=`, validates every response.
- `frontend/index.html` — "Battle Cards Generator" UI: single-company dashboard +
  head-to-head VS fight card, Owler light/dark theme, Demo/Live toggle, JSON export.
  See "UI / branding redesign" below. `frontend/index.legacy.html` = old version.
- `fixtures/` — `media_salesforce.com.json` (real captured) + `sample_*_response.json`
  (representative shapes for mapper tests) + `sample_stats_*_response.json` (stats).
- `fixtures/demo/` — QA's credible real-shape samples driving demo mode:
  `owler_sample.json`, `genai_lens_sample.json`, `media_search_sample.json`,
  `gong_transcripts_sample.json` (SF + HubSpot from Arun; Microsoft + Klue added here).
  Originals remain in repo-root `QA and Data/`.
- `tests/test_mappers.py` — 24 tests (mappers incl. demo real shapes, company-specific
  strengths, company-aware Gong, PII redaction, statistics, demo/live, schema validity).
- `docs/connector_access_request*.md` — access request (full + Slack).

## Done
- **Media (Meltwater unified_retrieval): REAL.** Live response mapping; transport =
  injectable `retrieval_fn` (default replays fixture; prod = `make_http_retrieval_fn`).
- **LLM synthesis chain:** prompt-per-section + Gong objection talk-tracks + confidence
  scoring + regenerate-below-0.5 guardrail. Transport = injectable `llm_fn`; resolves
  Anthropic (`anthropic_llm_fn`, needs `ANTHROPIC_API_KEY`) else deterministic stub.
  Robust JSON extraction + malformed-item drop.
- **Mappers built + tested:** `_map_owler`, `_map_genai_lens` (model-name→enum), `_map_gong`
  (PII anonymize → objection taxonomy → aggregate freq → top 5, no raw quotes).
- **Transport seams:** `owler_fn`/`genai_fn`/`gong_fn`/`retrieval_fn` thread through
  `generate_battlecard`→`fetch_all`→adapters. Inject one → that source goes live.
- **Honesty model:** mocks gated behind `BATTLECARD_ALLOW_MOCK` (default OFF →
  unconnected sources return None, card degrades, `overall_confidence` drops). Demo mode
  labels mock data "(simulated)" and excludes it from confidence.
- **Statistics enrichment (Media):** `build_volume_query`/`build_sentiment_query`,
  `_map_statistics` (shape-tolerant), and `stats_fn`/`sentiment_fn` threaded through
  `media_adapter`→`fetch_all`→`generate_battlecard`. Fills previously-null
  `news_feed.volume_trend` + refines sentiment_summary when provided; stays null
  otherwise (no fabrication). `make_http_statistics_fn` reads creds from env vars.
  13/13 tests pass; default card unchanged + schema-valid.
- **UI redesign + verified live:** new interactive frontend confirmed via Chrome to call
  `localhost:8000/battlecard` and render live API data (served over http; `file://` tabs
  can't be inspected by the Chrome tools).
- **Hackathon Meltwater MCP key assessed:** exposes document/statistics/profiles(influencer)
  /query_gen = **Media only**. Does NOT unblock Owler/GenAI Lens/Gong. JWT in shared config
  is still a `<JWT>` placeholder.
- **D617 BrightIdea submission updated:** Technologies Used (full stack) + Implementation
  Estimate = XL (solo-dev to production) filled and saved.

## Demo / Live dual-mode (2026-06-02)
QA (Arun) delivered credible, real-shaped samples for all 4 sources (`fixtures/demo/`).
The app now runs in two modes via `generate_battlecard(..., mode=)` / `?mode=`:
- **demo** — all 4 sources driven from the QA samples, mapped through the SAME
  mappers as live (so wiring real connectors later is a no-op). Tagged `_demo` →
  counts toward `overall_confidence` (0.85, full 4-source card) but every
  attribution is labeled "(sample data)". Mappers added: `_map_owler_real`,
  `_map_genai_lens_real`, `_map_gong_real` (keeps Arun's talk-tracks),
  `_map_media_search` (volume_trend[]→up/down/flat). Demo transports:
  `demo_owler_fn/genai_fn/gong_fn/media_fn` (+ new `media_fn` seam in media_adapter).
- **live** (default) — unchanged: connector transports, honest degrade.
UI: Demo/Live toggle in the header (amber/green dot, localStorage `bc_mode`) + a
sample-data banner. API: `?mode=demo|live` (422 on anything else).
Demo sample data covers **salesforce.com, hubspot.com, microsoft.com, klue.com**
(Arun supplied SF + HubSpot; Microsoft + Klue added to `fixtures/demo/` from the
shortlist — QA originals in `QA and Data/` left untouched). Unknown domains in
demo mode degrade honestly (no company borrows another's data) via
`_demo_pick_company`→None + `demo_domains()`. 21/21 tests pass.

## Head-to-head compare + company-specific claims (2026-06-02)
- Strengths/weaknesses are now derived per company (`_synth_claims` from GenAI Lens
  `aspect_detail` + media topics) instead of one hardcoded item — each company shows
  its own claims (e.g. HubSpot "Weak on enterprise scalability"). `_map_genai_lens_real`
  now carries `aspect_detail` (aspect/sentiment/score/phrases) for this.
- UI head-to-head: the second header input is an optional Company B *domain* picker
  (datalist of the demo companies). Empty → single card (unchanged). A second domain →
  two cards fetched and rendered as a dueling-bar comparison: confidence, revenue,
  headcount, founded, AI visibility, news sentiment (▲ marks the leader), parallel
  strengths/weaknesses columns, shared objections. Export saves both ({a,b}).
- Gong objections are company-aware: `_map_gong_real(resp, domain)` filters to calls
  whose `competitors_mentioned` include the target competitor (falls back to all when
  the company isn't referenced). Compare view shows objections for both sides.
- 24/24 tests pass.

## UI / branding redesign (2026-06-02) — `frontend/index.html`
Reworked the UI to be a professional "Battle Cards Generator" (single self-contained
HTML file; inline SVG/CSS charts, no external chart libs → works offline).
- **Title:** page + header renamed "Owler Battlecard Engine" → **"Battle Cards
  Generator"** (sub: "Powered by Owler · Meltwater media · GenAI Lens · Gong").
- **Two views, same render pipeline:**
  - *Single company* (Company B blank): identity header + confidence ring, "COMPETITIVE
    BATTLE CARD" eyebrow, a 5-tile **KPI strip** (revenue / headcount / AI visibility /
    news items / confidence), a 3-panel **statistics row** (news-sentiment donut from
    per-article sentiment, section-confidence bars, AI model visibility), then section
    cards (Strengths / Weaknesses / Objection Handling / AI Perception / News) under
    the tab filter.
  - *Head-to-head* (Company B = a second domain): **VS fight-card** — `.arena` with two
    colored corners + centered VS orb, "KEY METRICS" tale-of-the-tape ladder (winner dot
    + highlight per metric), a verdict pill ("X leads on N of 6 metrics"), per-company
    combat cards (Strengths/Weaknesses), objection-handling cards, and sentiment donuts.
    Builders: `tapeRow`, `bWin`, `blist`, `donut`, `kpiStrip`, `sectionConfidence`,
    `modelVis`, `renderCompare`, `render`.
- **Owler theme:** palette pulled from owler.com live (teal `#0BA2A2` primary, indigo
  `#0C1B88` secondary on white). Default = light Owler theme; dark theme retinted
  teal-on-navy (toggle persists in `localStorage.bc_theme`). Left card = deep teal,
  **right card = lighter teal-tint (`#a9e7e2→#7fd6d0`, dark text)**.
- **Professional wording** (combat metaphors removed): Strengths / Weaknesses /
  Objection handling / Key metrics / Head-to-head — no "Attacks/Vulnerabilities/Tale
  of the Tape/Beat".
- **Demo/Live parity:** one shared `bannerHtml()` shown in both modes (same layout,
  amber "Sample data" vs teal "Live data"); unified `fmtMoney()`/`fmtInt()` so a field
  formats identically whether the value is a raw number (live mapper) or pre-formatted
  string (demo) → both give "$34.9B" / "72,682".
- Verified headless (Node + DOM stubs) each change: both views render, 0 `undefined`
  leaks, `node --check` clean. NOTE: the user's browser can't reach the sandbox API,
  so live in-browser screenshots weren't taken — ask the user to eyeball
  `frontend/index.html` (API running) for final visual sign-off (esp. teal-tint
  contrast + VS centering).

## Blocked (external only)
Owler / GenAI Lens / Gong MCPs are NOT in the connector registry — internal Meltwater
tools needing admin provisioning. Need: the **HTTP endpoint + auth behind each MCP**
(service can't call session-bound MCP tools) + **one sample response each** to finalize
mappers. See `docs/connector_access_request.md`.

## Next steps (in order)
1. **Go live on statistics (hackathon window):** export `MELTWATER_MCP_URL` / `_JWT` /
   `_API_KEY`, build `stats_fn`/`sentiment_fn` via `make_http_statistics_fn`, pass to
   `generate_battlecard`. First real call: capture one statistics response into `fixtures/`
   to confirm `_map_statistics`' guessed envelope shape (`aggregations.by_time`/`by_sentiment`).
   Needs the real JWT (current config has a `<JWT>` placeholder).
2. Once Owler/GenAI/Gong land: write each `*_fn`, verify the `_map_*` against the real
   sample response, pass to `generate_battlecard`. One-liner each. (Not behind the
   hackathon key — still need admin provisioning; Gong is a separate vendor.)
3. Gong: confirm transcript shape vs `_iter_customer_lines`; add a proper NER pass to
   `_anonymize` (current baseline is regex + known names) before real transcripts.
4. Set `ANTHROPIC_API_KEY` to switch synthesis from stub to real model; add a
   prompt-injection guard (media article text flows into the LLM prompt).
5. Production hardening: Redis cache (in-memory won't survive restart/scale); secrets
   mgmt; graceful degradation instead of 500 on schema violation.
6. Minor: review "Anthropic" wording in saved D617 Technologies Used; optionally fill
   D617 Demo URL / GitHub Link.

## Run
`pip install -r requirements.txt` · `python3 aggregator.py` · `uvicorn api:app --reload`
· `open frontend/index.html` · tests: `python3 tests/test_mappers.py`
