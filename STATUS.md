# Battle Cards Generator — Project Status

**Phase:** CONSTRUCT — Production Hardening &nbsp;|&nbsp; **Last updated:** 2026-06-05

| Stat | Value |
|---|---|
| Test suite | ✅ 43 / 43 passing |
| Demo confidence | 0.85 (4-source card) |
| Demo companies | salesforce.com · hubspot.com · microsoft.com · klue.com |
| Auth | api-key (MCP endpoint; OAuth JWT not required) |

---

## Data Sources

| Source | Status | Notes |
|---|---|---|
| Media articles (Meltwater MCP) | ✅ Live | api-key auth confirmed; real SF Q1 FY27 data flowing |
| Media statistics (volume + sentiment) | ✅ Live | `volume_trend="up"`, `sentiment="positive"` |
| Owler | 🔴 Blocked | Needs admin provisioning — code seam wired and tested |
| GenAI Lens | 🔴 Blocked | Needs admin provisioning — code seam wired and tested |
| Gong | 🔴 Blocked | Needs admin provisioning — code seam wired and tested |
| Demo mode | ✅ Ready | QA sample data mapped through the same live mappers |

> Two sources are fully live. Three more are gated only on admin provisioning — all mapper code is complete and tested.

---

## Completed Features

**Data pipeline**
- Media articles + statistics fully live via api-key
- MCP JSON-RPC transport (SSE + JSON parse, confirmed in production)
- Owler / GenAI Lens / Gong mappers built + tested (PII anonymization, objection taxonomy, company-aware)
- Demo + Live dual-mode — QA samples run through the same mappers as live data
- `fetch_all` — concurrent 4-source fetch with per-source graceful degradation

**Synthesis + confidence**
- LLM synthesis chain — prompt-per-section, confidence scoring, regenerate-below-0.5 guardrail
- Anthropic transport (`anthropic_llm_fn`) — resolves from `ANTHROPIC_API_KEY`
- Prompt-injection guard (`_sanitize_for_prompt` at a single chokepoint)
- Schema violations return a `200 degraded` card, not a `500`

**UI + experience**
- Head-to-head compare — VS fight card, metric ladder, verdict pill
- Company-specific strengths / weaknesses via GenAI Lens `aspect_detail`
- Owler brand theme (teal `#0BA2A2` / indigo `#0C1B88`), light + dark mode, Demo / Live toggle

**Infrastructure**
- Redis cache — pluggable backend; Redis outage = cache miss, not `500`
- Live launcher (`scripts/run_local.sh`) — starts API + UI, tears down cleanly on Ctrl-C
- OAuth PKCE flow script (`scripts/get_meltwater_token.py`) — parked; api-key suffices

---

## Next Steps

These are ordered by priority. The first two unblock the most value.

| # | Item | Blocker |
|---|---|---|
| 1 | Land Owler / GenAI / Gong connectors | Admin provisioning — code seams ready |
| 2 | Set `ANTHROPIC_API_KEY` | Switches synthesis from stub to real model; injection guard already in place |
| 3 | Secrets management | Rotate, vault, confirm `.gitignore` — last remaining hardening item |
| 4 | Gong NER pass | Proper named-entity recognition before real transcripts flow |
| 5 | Visual sign-off | Teal-tint contrast + VS centering — eyeball with API running |

---

## Quick Start

```bash
pip install -r requirements.txt

# API only (port 8000)
uvicorn api:app --reload

# API + UI together
bash scripts/run_local.sh

# Run tests
python3 tests/test_mappers.py
```

---

## Architecture

```
domain
  └─► fetch_all (4 sources, concurrent)
        └─► normalize + map (per-source mappers)
              └─► LLM synthesis + confidence scoring
                    └─► schema-valid battlecard JSON (24 h cache)
```
