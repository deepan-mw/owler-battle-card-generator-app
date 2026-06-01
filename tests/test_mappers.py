"""Mapper + transport-seam tests. Exercise _map_owler / _map_genai_lens against
representative sample responses by injecting fetch transports, proving the live
wiring path produces schema-valid cards before real connectors exist.

    python3 -m pytest tests/ -q        # or: python3 tests/test_mappers.py
"""
import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import aggregator as A
import jsonschema

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCHEMA = json.load(open(os.path.join(_ROOT, "battlecard.schema.json")))
_FIX = os.path.join(_ROOT, "fixtures")


def _load(name):
    return json.load(open(os.path.join(_FIX, name)))


def _fetch_from(name):
    async def _fn(domain):
        return _load(name)
    return _fn


def test_map_owler_shape():
    out = A._map_owler(_load("sample_owler_response.json"))
    assert out["name"] == "Salesforce"
    assert out["revenue"] == "$31B"
    assert out["headcount"] == 73000
    assert out["hq"] == "San Francisco, CA"
    assert out["founded"] == 1999
    assert out["top_competitors"] == ["HubSpot", "Microsoft Dynamics", "Oracle", "Zoho"]


def test_map_genai_lens_model_enum():
    out = A._map_genai_lens(_load("sample_genai_lens_response.json"))
    assert out["visibility_score"] == 91 and out["vs_visibility_score"] == 58
    models = {m["model"] for m in out["model_breakdown"]}
    assert models == {"chatgpt", "claude", "gemini"}  # providers normalized to enum
    assert out["weakness_aspects"] == ["ease of use", "total cost"]


def test_injected_transports_produce_valid_card():
    card = asyncio.run(A.generate_battlecard(
        "salesforce.com", vs="HubSpot", fresh=True,
        owler_fn=_fetch_from("sample_owler_response.json"),
        genai_fn=_fetch_from("sample_genai_lens_response.json")))
    jsonschema.validate(card, _SCHEMA)
    # Real (non-mock) sources counted; owler + media + genai present, gong absent.
    assert set(card["meta"]["data_sources_used"]) == {"owler", "media", "genai_lens"}
    assert "(simulated)" not in json.dumps(card)  # injected data is real, not mock
    assert card["company_overview"]["ceo"] == "Marc Benioff"
    assert card["sections"]["ai_perception"]["visibility_score"] == 91
    assert card["meta"]["overall_confidence"] == round(3 / 4 * 0.85, 2)


def test_anonymize_redacts_pii():
    out = A._anonymize("Reach Jordan Lee at jordan.lee@vendor.com or 415-555-0192 via https://x.io",
                       names={"Jordan Lee"})
    assert "jordan.lee@vendor.com" not in out and "[EMAIL]" in out
    assert "415-555-0192" not in out and "[PHONE]" in out
    assert "Jordan Lee" not in out and "[NAME]" in out
    assert "[URL]" in out


def test_map_gong_extracts_and_aggregates():
    out = A._map_gong(_load("sample_gong_response.json"))
    objs = out["objections"]
    assert objs and len(objs) <= 5
    # raw transcript PII must never survive into objection statements
    blob = json.dumps(out)
    for leak in ("Priya", "Wei Zhang", "@vendor.com", "415-555-0192", "@acme.io"):
        assert leak not in blob, f"PII leak: {leak}"
    # pricing appears in both calls -> highest frequency
    assert objs[0]["objection"].startswith("Pricing") and objs[0]["frequency"] == 2
    cats = {o["objection"] for o in objs}
    assert any("complex" in c.lower() for c in cats)


def test_gong_injected_transport_valid_card():
    card = asyncio.run(A.generate_battlecard(
        "salesforce.com", vs="HubSpot", fresh=True,
        gong_fn=_fetch_from("sample_gong_response.json")))
    jsonschema.validate(card, _SCHEMA)
    assert "gong" in card["meta"]["data_sources_used"]
    items = card["sections"]["objections"]["items"]
    assert items and all({"objection", "talk_track", "confidence"} <= set(i) for i in items)


def test_build_media_query():
    q = A.build_media_query("salesforce.com", days=7)
    assert q["platforms"] == ["news"] and q["startDate"] < q["endDate"]
    assert "salesforce" in q["query"]


def test_build_stats_queries():
    v = A.build_volume_query("salesforce.com", days=14)
    s = A.build_sentiment_query("salesforce.com", days=14)
    assert v["platforms"] == ["news"] and v["startDate"] < v["endDate"]
    assert "count" in v["query"].lower() and "sentiment" in s["query"].lower()


def test_map_statistics_volume_and_sentiment():
    stats = A._map_statistics(_load("sample_stats_volume_response.json"),
                              _load("sample_stats_sentiment_response.json"))
    assert stats["volume_trend"] == "up"      # 4+5 older vs 11+14 newer -> rising
    assert stats["sentiment"] == "positive"   # 38 pos vs 9 neg


def test_map_statistics_missing_inputs_degrade_to_none():
    # honesty model: no fabrication when stats unavailable/unparseable
    assert A._map_statistics(None, None) == {"volume_trend": None, "sentiment": None}
    assert A._map_statistics([{"type": "text", "text": "{}"}], None)["volume_trend"] is None


def test_media_adapter_enriched_with_stats():
    media = asyncio.run(A.media_adapter(
        "salesforce.com",
        stats_fn=_fetch_from("sample_stats_volume_response.json"),
        sentiment_fn=_fetch_from("sample_stats_sentiment_response.json")))
    assert media["volume_trend"] == "up"        # was always None before enrichment
    assert media["sentiment"] == "positive"     # overrides article heuristic
    assert media["articles"]                    # base media still intact


def test_stats_injected_card_valid():
    card = asyncio.run(A.generate_battlecard(
        "salesforce.com", vs="HubSpot", fresh=True,
        stats_fn=_fetch_from("sample_stats_volume_response.json"),
        sentiment_fn=_fetch_from("sample_stats_sentiment_response.json")))
    jsonschema.validate(card, _SCHEMA)
    nf = card["sections"]["news_feed"]
    assert nf["volume_trend"] == "up" and nf["sentiment_summary"] == "positive"


def test_default_mode_still_honest():
    card = asyncio.run(A.generate_battlecard("hubspot.com", vs="Salesforce", fresh=True))
    jsonschema.validate(card, _SCHEMA)
    assert card["meta"]["company_name"] == "hubspot.com"  # no fabricated owler name
    assert card["meta"]["data_sources_used"] == []
    assert card["meta"]["overall_confidence"] == 0.0


# --- Demo mode: QA sample data mapped through the real pipeline -----------

def test_map_owler_real_shape():
    data = A._demo_pick_company(A._demo_load("owler_sample.json"), "salesforce.com")
    out = A._map_owler(data)
    assert out["name"] == "Salesforce"
    assert out["revenue"] == "$34.9B"
    assert out["headcount"] == 72682
    assert out["ceo"] == "Marc Benioff"
    assert out["founded"] == 1999
    assert "Hubspot" in out["top_competitors"]


def test_map_genai_lens_real_shape():
    data = A._demo_pick_company(A._demo_load("genai_lens_sample.json"), "salesforce.com")
    out = A._map_genai_lens(data)
    assert out["visibility_score"] == 88
    assert {m["model"] for m in out["model_breakdown"]}  # populated
    assert out["weakness_aspects"]  # Ease of Use / Pricing are negative


def test_map_media_search_real_shape():
    data = A._demo_pick_company(A._demo_load("media_search_sample.json"), "salesforce.com")
    out = A._map_media_search(data)
    assert out["sentiment"] == "positive"
    assert out["volume_trend"] in ("up", "down", "flat")
    assert out["articles"] and out["articles"][0]["headline"]


def test_map_gong_real_shape_keeps_talktrack():
    out = A._map_gong(A._demo_load("gong_transcripts_sample.json"))
    assert out["objections"]
    top = out["objections"][0]
    assert top["frequency"] >= 1 and top.get("talk_track")


def test_demo_mode_full_card_valid_and_labeled():
    card = asyncio.run(A.generate_battlecard("salesforce.com", vs="HubSpot",
                                             mode="demo", fresh=True))
    jsonschema.validate(card, _SCHEMA)
    m = card["meta"]
    assert set(m["data_sources_used"]) == {"owler", "media", "genai_lens", "gong"}
    assert m["overall_confidence"] == 0.85  # sample data counts toward confidence
    labels = [a["label"] for a in card["company_overview"]["attribution"]]
    assert labels and all("(sample data)" in l for l in labels)
    assert "(simulated)" not in json.dumps(card)
    obj = card["sections"]["objections"]["items"]
    assert obj and any("(sample data)" in a["label"]
                       for i in obj for a in i.get("attribution", []))


def test_demo_mode_multiple_companies():
    # all shortlisted demo companies produce a full, schema-valid 4-source card
    for dom, name in [("salesforce.com", "Salesforce"), ("hubspot.com", "HubSpot"),
                      ("microsoft.com", "Microsoft"), ("klue.com", "Klue")]:
        card = asyncio.run(A.generate_battlecard(dom, mode="demo", fresh=True))
        jsonschema.validate(card, _SCHEMA)
        assert card["meta"]["company_name"] == name
        assert set(card["meta"]["data_sources_used"]) == {"owler", "media", "genai_lens", "gong"}


def test_demo_mode_unknown_domain_degrades_honestly():
    # a domain with no sample must NOT borrow another company's data
    card = asyncio.run(A.generate_battlecard("oracle.com", mode="demo", fresh=True))
    jsonschema.validate(card, _SCHEMA)
    assert card["meta"]["data_sources_used"] == []
    assert card["meta"]["overall_confidence"] == 0.0
    assert card["meta"]["company_name"] == "oracle.com"  # no fabricated name


def test_strengths_are_company_specific():
    # strengths/weaknesses must derive from each company's own GenAI aspects,
    # not a shared hardcoded claim (regression: stub used to return Salesforce text)
    cards = {d: asyncio.run(A.generate_battlecard(d, mode="demo", fresh=True))
             for d in ("salesforce.com", "hubspot.com", "microsoft.com", "klue.com")}
    tops = {d: c["sections"]["strengths"]["items"][0]["claim"] for d, c in cards.items()}
    assert len(set(tops.values())) >= 3            # not all identical
    assert "ecosystem" in tops["salesforce.com"].lower()
    assert "ease of use" in tops["hubspot.com"].lower()
    assert "competitive intelligence" in tops["klue.com"].lower()
    # every company has at least one weakness (incl. low-scoring neutrals)
    for d, c in cards.items():
        assert c["sections"]["weaknesses"]["items"], f"{d} has no weaknesses"


def test_gong_objections_are_company_aware():
    raw = A._demo_load("gong_transcripts_sample.json")
    sf = A._map_gong_real(raw, "salesforce.com")["objections"]
    hs = A._map_gong_real(raw, "hubspot.com")["objections"]
    sf_txt, hs_txt = " ".join(o["objection"] for o in sf), " ".join(o["objection"] for o in hs)
    assert "Salesforce" in sf_txt and "Salesforce" not in (hs_txt.replace("Salesforce and HubSpot", ""))
    assert "HubSpot" in hs_txt
    assert sf_txt != hs_txt  # different competitors → different objections
    # a company not referenced in any call falls back to all objections (non-empty)
    assert A._map_gong_real(raw, "microsoft.com")["objections"]


def test_live_mode_unchanged():
    card = asyncio.run(A.generate_battlecard("hubspot.com", mode="live", fresh=True))
    jsonschema.validate(card, _SCHEMA)
    assert card["meta"]["overall_confidence"] == 0.0
    assert "(sample data)" not in json.dumps(card)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"PASS {name}")
    print("ALL MAPPER TESTS PASSED")
