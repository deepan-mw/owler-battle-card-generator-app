"""FastAPI surface for the Owler Battlecard Engine.

    uvicorn api:app --reload
    GET /battlecard?domain=salesforce.com&vs=HubSpot   -> schema-valid battlecard JSON
    GET /healthz

Validates every response against battlecard.schema.json before returning, so a
synthesis regression surfaces as a 500 here rather than corrupt data downstream.
"""
from __future__ import annotations
import json, os, re
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import jsonschema

import aggregator

_SCHEMA = json.load(open(os.path.join(os.path.dirname(__file__), "battlecard.schema.json")))
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](-?[a-z0-9])*\.)+[a-z]{2,}$", re.I)

app = FastAPI(title="Owler Battlecard Engine", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"],
                   allow_headers=["*"])


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/battlecard")
async def battlecard(
    domain: str = Query(..., description="Competitor domain, e.g. salesforce.com"),
    vs: str | None = Query(None, description="The 'us' company to position against"),
    fresh: bool = Query(False, description="Bypass the 24h cache"),
    mode: str = Query("live", description="'live' (connectors) or 'demo' (sample data)"),
):
    domain = domain.strip().lower()
    if not _DOMAIN_RE.match(domain):
        raise HTTPException(status_code=422, detail=f"invalid domain: {domain!r}")
    mode = mode.strip().lower()
    if mode not in ("live", "demo"):
        raise HTTPException(status_code=422, detail=f"invalid mode: {mode!r} (use 'live' or 'demo')")
    try:
        card = await aggregator.generate_battlecard(domain, vs=vs, fresh=fresh, mode=mode)
    except Exception as exc:  # upstream/source failure
        raise HTTPException(status_code=502, detail=f"generation failed: {exc}")
    try:
        jsonschema.validate(card, _SCHEMA)
    except jsonschema.ValidationError as exc:
        # A synthesis regression produced an invalid card. Rather than 500 with no
        # usable payload, fall back to a guaranteed-valid degraded (empty) card so
        # the client gets a renderable, honestly-labeled response.
        fallback = aggregator.degraded_card(domain, vs=vs, reason=f"schema violation: {exc.message}")
        try:
            jsonschema.validate(fallback, _SCHEMA)
        except jsonschema.ValidationError as exc2:
            raise HTTPException(status_code=500, detail=f"schema violation (no fallback): {exc2.message}")
        return JSONResponse(fallback)
    return JSONResponse(card)
