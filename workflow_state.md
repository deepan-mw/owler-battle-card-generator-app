# workflow_state.md — Owler Battlecard Engine

## Phase
CONSTRUCT — Demo/Live dual-mode (plan approved 2026-06-02)

## Plan (2026-06-02) — Two-mode demo support from Arun's sample data
Arun delivered credible, real-shaped sample responses for all four sources in
`QA and Data/` (owler_sample, genai_lens_sample, media_search_sample,
gong_transcripts_sample). Use them to run the app in two modes:
- **Demo mode**: sample data mapped through the real mappers, labeled
  "(sample data)", counted toward confidence (realistic card for presentations).
- **Live mode**: existing connector transports (unchanged; still degrade honestly).

Steps:
1. Copy the four QA files into `fixtures/demo/` (self-contained app).
2. Extend mappers to accept Arun's real shapes (single mapping point, so when
   live connectors land in these shapes they just work):
   - `_map_owler`: handle `owler_company_details` wrapper (employee_count,
     revenue_usd, headquarters{city,state}, executives[0]→ceo, competitors, products).
   - `_map_genai_lens`: handle `genai_lens_brandAnalysis` + `entityAspectProfile`
     (overall_visibility_score, brand_description_consensus, aspects→strength/weakness).
   - `_map_gong`: handle pre-extracted `objections` in `nx_get_gong_curated_transcript`,
     aggregate by category across calls; PII anonymize still applies to any free text.
   - new `_map_media_search`: map `mira_search_media_news` (articles, sentiment_summary,
     volume_trend[]→up/down/flat via _volume_trend_from_series, topics).
3. Demo transports + `mode` param threaded through generate_battlecard/fetch_all.
   Sample sources tagged `_demo`; counted in overall_confidence; attribution
   labeled "(sample data)" via `_mark_simulated` extension.
4. API: `GET /battlecard?mode=demo|live` (default live). Frontend: Demo/Live
   header toggle + "SAMPLE DATA" badge.
5. Tests: demo card 4-source, schema-valid, sample-labeled, confidence>0; live
   unchanged.

## Decisions (2026-06-02)
- Mode switch: API param + UI toggle (demo-friendly; flip live on stage). (Claude, user deferred)
- Confidence: demo/sample data counts toward confidence but is visibly labeled
  "(sample data)" — distinct from `_mock` blobs which stay excluded. (Claude, user deferred)
- Mappers absorb Arun's shapes directly (not pre-transformed) so live wiring is a no-op.

## Plan (prior)
CONSTRUCT (plan approved 2026-06-01)

## Plan
Continue developer tasks in priority order, keeping every output schema-valid.

1. **Media adapter → real call.** Replace `media_adapter` mock with the
   `unified_retrieval_document_retrieval_tool` response shape. Transport is an
   injectable async hook (`retrieval_fn`); default loads a live-data fixture
   captured this session, falls back to mock on any failure. Map resources →
   `news_feed` items (headline, url, source, published_at, topic_tag, sentiment),
   derive `sentiment_summary` heuristically, `volume_trend` left null (no baseline).
   - Rationale: the Python process cannot call the MCP tool at runtime (the tool
     is session-bound). The genuine engineering is the response mapping + transport
     seam; in production `retrieval_fn` becomes the HTTP call to the retrieval API.
2. **Owler / GenAI Lens / Gong adapters.** Not connected this session. Refactor
   each into the same real-call-or-fallback structure with TODO intact, so wiring
   the real MCP/HTTP call later is a one-line swap. Bodies stay mock.
3. **LLM synthesis chain.** `_synth_*` → real prompt-per-section templates +
   Gong objection extraction + confidence scoring + regenerate-if-below-0.5
   guardrail. Model call is an injectable function (`llm_fn`), default = the
   existing deterministic heuristic so the service runs end-to-end with no creds.
4. **FastAPI endpoint** `GET /battlecard?domain=&vs=` wrapping `generate_battlecard`.
5. **Frontend component** rendering the 6 sections with attribution + confidence,
   pointed at the API.

VALIDATE after every change: `generate_battlecard("salesforce.com", vs="HubSpot")`
+ `jsonschema.validate` against `battlecard.schema.json`.

## Decisions
- Scope: wire Media for real now; other three mock-with-clean-swap. (user)
- LLM runtime: stub the model call, real prompts/parsing/guardrail. (user)

## Open items / risks
- Owler, GenAI Lens, Gong MCPs not connected → adapters remain mock until added.
- Retrieval API has no per-article sentiment → sentiment derived heuristically.

## Log (2026-06-02h) — right card teal-tint + Demo/Live parity
- Right-side (company B) card changed from indigo to a lighter Owler teal-tint
  (#a9e7e2→#7fd6d0) with dark-teal text + teal avatar; comparison bar retinted
  (--red #5fcbc5). Left stays deep teal — clear but on-brand contrast.
- Demo/Live consistency: replaced the demo-only banner with a shared bannerHtml()
  shown in BOTH modes, identical layout, color/label by mode (amber "Sample data" /
  teal "Live data"). Unified value formatting via fmtMoney()/fmtInt() so the same
  field renders identically regardless of source shape (number from a live mapper or
  pre-formatted string from demo both yield "$34.9B" / "72,682"); applied in KPI
  strip, overview grid, and the comparison ladder. Verified: formatter parity,
  both modes render, 0 undefined leaks, node --check clean.

## Log (2026-06-02g) — Owler theme + professional wording + title
- Pulled owler.com live palette via Chrome (teal #0BA2A2, indigo #0C1B88 on white).
  Rethemed app: default light Owler theme (white surfaces, teal glow, teal/indigo
  accents); dark theme retinted teal-on-navy. Corners now teal (A) vs indigo (B).
- Professionalized wording (dropped combat metaphors): Tale of the Tape → KEY METRICS
  (with HEAD-TO-HEAD kept only in the centered VS emblem), Attacks/Vulnerabilities →
  Strengths/Weaknesses, "Beat X" → "Objection handling · X", verdict → "X leads on N
  of 6 metrics". Single view: "COMPETITIVE BATTLE CARD" eyebrow, Strengths/Weaknesses/
  Objection Handling, tabs renamed.
- Title renamed: page + header "Owler Battlecard Engine" → "Battle Cards Generator"
  (sub: "Powered by Owler · …"). VS centered as an orb emblem. Right-side (company B)
  card brightened to a richer Owler indigo (#3a49c4→#162a9c). Verified headless:
  markup checks pass, both views render, 0 undefined leaks, node --check clean.

## Log (2026-06-02f) — "Battle Card" identity redesign
- Reworked the UI to justify the product name. Head-to-head is now a VS fight card:
  blue-corner vs red-corner banner with a central VS emblem, a "TALE OF THE TAPE"
  stat ladder (confidence/revenue/headcount/founded/AI visibility/sentiment) with a
  winner dot + highlight per metric and a tallied verdict pill ("X leads 4–1"),
  then combat cards per company (Attacks = strengths, Vulnerabilities = weaknesses),
  objection-counter cards ("Beat X"), and the sentiment donuts. Single-company view
  rebranded: "⚔️ COMPETITIVE BATTLE CARD" eyebrow, sections renamed Attacks /
  Vulnerabilities / Objection Counters (tabs too). New battle CSS (.arena/.corner/
  .vsbig/.tape/.combat/.ccard/.blist/.verdict). Verified headless: both views render
  (corners, tale, verdict, attacks/vulns/counters), 0 undefined leaks, node --check clean.

## Log (2026-06-02e) — dashboard redesign (single-company view)
- Reworked render() into a professional dashboard: identity header + confidence ring,
  a 5-tile KPI strip (revenue / headcount / AI visibility / news items / confidence),
  and a 3-panel statistics row — news-sentiment donut (computed from per-article
  sentiment, falls back to sentiment_summary), section-confidence bars (all 5
  sections), and AI model visibility ranks. Qualitative section cards remain below
  under the existing tab filter. New CSS (.kpi/.stats/.donut/.sconf/.mvis), inline
  SVG donut (no external chart lib → works offline). sconf bars animate on paint.
  Verified headless: KPI strip, stats row, all KPIs, donut, section confidence, model
  visibility present; 0 undefined leaks; node --check clean.
- Consistency: compare view now reuses the sentiment donut (one per company) in
  styled .panel blocks, matching the dashboard's statistics components. Verified
  headless: two donuts render, 0 undefined leaks.

## Log (2026-06-02d) — Gong objections company-aware
- _map_gong_real now takes `domain`: builds call_id→competitors_mentioned from
  nx_list_gong_calls and keeps only objections from calls mentioning the target
  competitor; falls back to all deal-level objections when the company isn't
  referenced (e.g. microsoft.com). Threaded domain through _map_gong + gong_adapter.
  Result: Salesforce/HubSpot/Klue each surface their own objections; compare view
  now shows objections for BOTH sides (two columns). +1 test (24/24 pass).

## Log (2026-06-02c) — section completeness + head-to-head UI
- Verified all sections populated in demo; found strengths/weaknesses were a single
  hardcoded Salesforce-flavored item for every company. Fixed: _map_genai_lens_real
  now returns aspect_detail (aspect/sentiment/score/phrases); new _synth_claims builds
  company-specific strengths (positive + high neutral) and weaknesses (negative + low
  neutral) from GenAI aspects + media topics, multi-item, with a generic fallback.
  Each company now differs (e.g. HubSpot → "Weak on enterprise scalability").
- Head-to-head UI: #vs repurposed as optional Company B domain picker (datalist of
  demo cos). Empty B → single card (unchanged); B with a "." → fetch both cards and
  renderCompare(): dueling-bar metric rows (confidence/revenue/headcount/founded/AI
  visibility/sentiment with ▲ winner), parallel strengths/weaknesses columns, shared
  objections. Export handles compare ({a,b}, combined filename).
- VALIDATE: 23/23 tests pass (+2: company-specific strengths, prior demo set). API
  serves both cards; renderCompare verified in Node with real JSON — VS pill, all 6
  rows, tailored strengths, talk-tracks, export enabled, 0 undefined leaks. JS
  node --check clean.

## Log (2026-06-02b)
- Docs + demo company expansion. README gained a "Demo vs Live mode" section.
  Added Microsoft + Klue to fixtures/demo/{owler,genai_lens,media}_sample.json
  (from demo_company_shortlist Tier 1+2; QA originals untouched). Fixed silent
  fallback: _demo_pick_company returns None (no borrowing another company's data),
  demo_*_fn raise LookupError for unsupported domains, gong gated via demo_domains().
  Demo now serves salesforce/hubspot/microsoft/klue full 4-source cards; unknown
  domains (e.g. oracle.com) degrade to 0 sources/0 conf. +2 tests (21/21 pass).

## Log (2026-06-02)
- Demo/Live dual-mode shipped. Copied Arun's 4 QA samples into fixtures/demo.
  Extended mappers for the real shapes (_map_owler_real, _map_genai_lens_real,
  _map_gong_real, new _map_media_search) — same mappers serve live + demo.
  Demo transports (demo_owler/genai/gong/media_fn) + media_fn seam through
  media_adapter/fetch_all. generate_battlecard gained mode="live|demo"; demo tags
  sources _demo → counted in confidence, labeled "(sample data)" (distinct from
  _mock "(simulated)"). API: ?mode=demo|live (422 on bad). Frontend: Demo/Live
  toggle (localStorage bc_mode, dot indicator) + amber sample-data banner.
  VALIDATE: 19/19 mapper tests pass (6 new); demo card = 4 sources/conf 0.85/
  schema-valid/all labels "(sample data)"/no "(simulated)"; live unchanged
  (media only, 0.21); API TestClient demo=200, live=200, bogus mode=422.

## Log
- 2026-06-01: Read CLAUDE.md, schema, example, aggregator.py, README. Confirmed
  only `unified_retrieval` (Media) connected; Owler/GenAI/Gong tools absent.
- 2026-06-01: Probed live `unified_retrieval_document_retrieval_tool` for
  salesforce.com (news, last 7d). Captured real response shape + saved fixture
  (project/fixtures/media_salesforce.com.json).
- 2026-06-01: Task 1 — rewrote media_adapter to parse the real envelope, map to
  news_feed (source/sentiment/topic per article), injectable retrieval_fn, mock
  fallback. Validates schema-clean on live data (5 articles).
- 2026-06-01: Task 2 — refactored owler/genai_lens/gong adapters to
  real-or-fallback shape, TODOs intact.
- 2026-06-01: Task 3 — LLM chain: prompt-per-section templates, default
  deterministic llm_fn, _score_section + regenerate-below-0.5 guardrail.
- 2026-06-01: Task 4 — api.py FastAPI: GET /battlecard, schema validation,
  422/502/500 handling. TestClient: ok=200, no-vs=200, bad=422, missing=422.
- 2026-06-01: Task 5 — frontend/index.html renders 6 sections, attribution chips,
  confidence bars; API fetch with sample fallback.
- 2026-06-01: VALIDATE — fixed latent bug (vs_company=None violated schema; now
  omitted when absent). 6-check verification suite + API codes all pass.

## VALIDATE — result: PASS
Schema-valid on: real-data card, custom llm_fn, retrieval failure fallback,
unknown-domain, vs-omitted. Cache identity confirmed. API 200/422 correct.

- 2026-06-01: Task 3+ — real LLM transport. `anthropic_llm_fn` (Messages API),
  `_resolve_llm_fn` (override > Anthropic if ANTHROPIC_API_KEY+pkg > stub),
  `_extract_json` (handles fenced/prose-wrapped JSON), per-section required-key
  filter so malformed model output degrades gracefully. Added `anthropic` to
  requirements (optional). Re-verified: resolver, json-extraction, section-aware
  prose model, malformed-item drop, transport-failure fallback, no-creds default —
  all schema-valid.
- 2026-06-01: Hardening — fixed mock-fabrication issue. BATTLECARD_ALLOW_MOCK gate
  (default OFF → owler/genai/gong/media-fallback return None). `_mock` tag +
  `_mark_simulated` labels mock attribution "(simulated)"; overall_confidence now
  uses real sources only; overview/ai attribution empty when source absent. Added
  `_map_owler/_map_genai_lens/_map_gong` mapper stubs as explicit swap points.
  Verified: hubspot.com no longer returns Salesforce data; both modes schema-valid;
  salesforce default = media real (5 articles, conf 0.21).
- 2026-06-01: Connection-readiness. Owler/GenAI Lens not in MCP registry (internal
  Meltwater tools → need admin provisioning; same gate as Gong). Built code-side:
  per-source injectable transports (owler_fn/genai_fn/gong_fn) threaded through
  fetch_all + generate_battlecard; real _map_owler + _map_genai_lens (model-name
  enum normalization, shape-tolerant); build_media_query + make_http_retrieval_fn
  production helper; sample fixtures + tests/test_mappers.py (5 tests). _map_gong
  still stub (NLP + PII). All mapper tests pass; full regression clean (default,
  demo-simulated, owler-only injected, API 200).
- 2026-06-01: Gong changes. _map_gong implemented: _anonymize (emails/phones/URLs/
  participant names) runs before processing per no-PII policy; objection extraction
  via _OBJECTION_CUES taxonomy, aggregated by category across calls (frequency +
  call_ref), top 5; emits templated statements only (no raw quotes). Sample fixture
  with synthetic PII + 3 new tests (anonymize, extract/aggregate, injected card).
  8/8 mapper tests pass; 4-source injected card schema-valid, conf 0.85, no PII leak.
  Drafted docs/connector_access_request.md for admin (Owler/GenAI/Gong provisioning
  + HTTP endpoint/auth + sample responses + Gong governance Qs).
- 2026-06-01: Hackathon Meltwater MCP key assessed. Probed connected
  unified_retrieval suite: document/statistics/profiles(influencer)/query_gen — Media
  only. Does NOT cover Owler/GenAI Lens/Gong (Gong is separate vendor). Key does not
  unblock those sources; used it to plan Media enrichment instead.
- 2026-06-01: Media enrichment via statistics_retrieval. Added build_volume_query +
  build_sentiment_query, _unwrap_envelope (shared), _map_statistics (shape-tolerant
  _find_buckets → _volume_trend_from_series older/newer-half ratio, _sentiment_from_
  breakdown). media_adapter gained stats_fn/sentiment_fn (override volume_trend +
  sentiment when provided); threaded through fetch_all + generate_battlecard.
  make_http_statistics_fn helper (env-var creds, adds api-key header). Fills the
  previously-null news_feed.volume_trend. Honest degrade: no stats → field stays null.
  Added 2 stats fixtures + 5 tests (13/13 pass); default card unchanged, schema-valid.
- 2026-06-01: UI redesign. Rebuilt frontend/index.html (old saved as index.legacy.html):
  hero with avatar + SVG confidence ring, sticky glass header, light/dark toggle
  (localStorage), section tab filter (All/Overview/Strengths/Weaknesses/Objections/
  AI/News), news sentiment filter buttons, animated confidence/visibility bars, source
  legend (dims unused sources), simulated badge, skeleton loading, JSON export. Same
  API contract + sample fallback. Verified headless: full + degraded (null conf, missing
  sections, no vs) render with no JS errors; tabs/filters/ring/talk-track present.
