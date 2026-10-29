"""The call screen: the request reader, the routes' checks on what they are sent, and the whole path
on a loaded snapshot.

No model is called: the request reader is given a scripted reply, as the model's tool call would be.
"""
from types import SimpleNamespace as NS

import pytest

from app import auth, create_app
from engine import benefits as bn
from engine import plan as planning
from engine.catalog import Catalog
from engine.db import Snapshot
from tests.test_catalog import _rows
from tests.test_engine_answer import needs_db


def _client_reply(**tool_input):
    reply = NS(usage=None, content=[NS(type="tool_use", name="plan_request", input=tool_input)])
    return NS(messages=NS(create=lambda **kw: reply))


def test_the_request_reader_keeps_only_allowed_values():
    p = planning.plan("x", client=_client_reply(
        navy_terms=["gold 350", ""], comp_terms=["HCF"], features=["price", "cover:Teleportation", "tier"],
        state="ALL", family_type="couple", product_type="dental", confidence=0.9), model="m")
    assert p.navy_terms == ["gold 350"] and p.comp_terms == ["HCF"]
    assert p.features == ["price", "tier"]
    assert p.state == "" and p.product_type == "" and p.family_type == "couple"


def test_a_reply_without_the_tool_call_is_an_empty_plan():
    client = NS(messages=NS(create=lambda **kw: NS(usage=None, content=[NS(type="text", text="hello")])))
    p = planning.plan("x", client=client, model="m")
    assert p.navy_terms == [] and p.comp_terms == [] and p.confidence == 0


def test_an_exact_name_is_ticked_for_the_rep_and_a_loose_one_is_offered():
    cat = Catalog.from_rows(_rows(), 1)
    offer = planning.suggestions(cat, planning.Plan(navy_terms=["Premium Gold Hospital 750", "gold"]))
    exact, loose = offer["navy"]
    assert exact["pick"] == [3]
    assert loose["pick"] == [] and {p["id"] for p in loose["policies"]} == {2, 3}


def test_a_company_term_offers_the_company():
    cat = Catalog.from_rows(_rows(), 1)
    offer = planning.suggestions(cat, planning.Plan(comp_terms=["HCF"]))
    assert offer["comp"][0]["funds"][0]["code"] == "HCF"
    assert offer["comp"][0]["pick"] == [4]   # HCF has one policy here, so it is the only match


def test_all_navy_policies_are_offered_but_not_ticked():
    cat = Catalog.from_rows(_rows(), 1)
    offer = planning.suggestions(cat, planning.Plan(navy_all=True))
    assert {p["id"] for p in offer["navy"][0]["policies"]} == {1, 2, 3}
    assert offer["navy"][0]["pick"] == []


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "log_event", lambda **kw: None)
    rep = auth.User(id="00000000-0000-0000-0000-000000000001", email="rep@example.com", full_name="Pat Rep",
                    phone=None, status="approved", is_admin=False, modules={"call_screen"})
    monkeypatch.setattr(auth, "resolve", lambda at, rt: auth.Resolved(user=rep))
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


def test_benefit_search_answers_without_a_database(client):
    r = client.get("/search/benefits?q=root canal")
    assert r.status_code == 200
    assert "item:Endodontic417RootCanalTherapy" in [b["key"] for b in r.get_json()["benefits"]]


def test_the_card_refuses_an_unknown_state_or_household(client):
    assert client.get("/card?ids=1&state=XX&family=couple").status_code == 400
    assert client.get("/card?ids=1&state=NSW&family=nobody").status_code == 400


def test_the_card_asks_for_a_policy_when_none_is_given(client):
    r = client.get("/card?ids=&state=NSW&family=couple")
    assert b"Pick at least one policy" in r.data


def test_the_plan_route_needs_the_form_token(client):
    assert client.post("/call/plan", data={"request": "gold"}).status_code == 400


def test_a_signed_out_caller_cannot_search(monkeypatch):
    monkeypatch.setattr(auth, "log_event", lambda **kw: None)
    app = create_app()
    r = app.test_client().get("/search?q=hcf")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


@needs_db
def test_one_fetch_returns_every_part_for_three_policies():
    db = Snapshot()
    data = bn.fetch(db, ids=[2789, 2330, 115], state="NSW", family="couple", scope="")
    assert len(data["policies"]) == 3 and data["release_date"]
    assert len([c for c in data["cover"] if c["policy_id"] == 2789]) == 38
    vals = bn.values(data)
    assert vals[2789]["price"].note == "at $500 excess"
    assert vals[115]["price"].display == "$278.08 a month"


@needs_db
def test_the_card_for_a_real_pair_puts_decisive_cover_first(client):
    r = client.get("/card?ids=2789,115&state=NSW&family=couple")
    html = r.data.decode()
    assert r.status_code == 200
    assert "Navy Health covers, " in html and "Back, neck and spine" in html
    assert "$278.08 a month" in html and "at $750 excess" in html
    assert "a year" in html and 'class="sheet-gap' in html     # the price gap, said a year as well as a month


@needs_db
def test_a_competitor_alone_is_compared_with_its_closest_navy_policy(client):
    import json
    import re
    html = client.get("/card?ids=115&state=NSW&family=couple").data.decode()
    meta = json.loads(re.search(r'<script type="application/json" id="card-meta">(.*?)</script>', html, re.S).group(1))
    assert meta["auto_navy"] and meta["auto_navy"]["id"] in meta["columns"]
    assert "best match" in html and 'class="sheet-gap' in html
    assert all(a["id"] != meta["auto_navy"]["id"] for a in meta["alternatives"])


@needs_db
def test_the_index_finds_real_companies_and_policies():
    from engine.catalog import catalog
    c = catalog(Snapshot())
    assert c.search("hcf", side="comp")["funds"][0]["code"] == "HCF"
    assert any("Silver+" in p["name"] for p in c.search("silver+", side="navy")["policies"])


@needs_db
def test_the_rep_can_match_premiums_at_another_excess(client):
    html = client.get("/card?ids=2789,2330,115&state=NSW&family=couple").data.decode()
    assert "excess closest to $500" in html and "excess-choice" in html
    html = client.get("/card?ids=2789,2330,115&state=NSW&family=couple&excess=750").data.decode()
    assert "excess closest to $750" in html


def test_the_talking_point_facts_keep_each_side_in_its_own_field():
    from engine.benefits import Value
    from engine.differences import card
    from engine.script import facts
    vals = {
        1: {"price": Value("price", "premium", "$380 a month", -380.0, 380.0, "dollars", "at $500 excess"),
            "cover:BackNeckSpine": Value("cover:BackNeckSpine", "cover_category", "Covered", 2.0, 2.0)},
        2: {"price": Value("price", "premium", "$300 a month", -300.0, 300.0, "dollars", "at $750 excess"),
            "cover:BackNeckSpine": Value("cover:BackNeckSpine", "cover_category", "Not covered", 0.0, 0.0)},
    }
    rows = facts(card(1, 2, vals), navy_name="Core Silver+ Hospital", comp_name="HCF Bronze Plus", snapshot=1,
                 state="NSW", family="couple")
    price, spine = rows
    assert price["value"] == {"navy": 380.0, "comp": 300.0} and price["unit"] == "AUD/month"
    assert price["claimable"]["navy_excess"] == 500.0 and price["claimable"]["comp_excess"] == 750.0
    assert spine["value"] == {"navy": "Covered", "comp": "Not covered"}
    assert "Core Silver+ Hospital" in spine["names"] and "HCF Bronze Plus" in spine["names"]
    from ai.facts import Fact
    assert all(Fact.model_validate(r) for r in rows)


def test_the_talking_point_needs_the_form_token(client):
    assert client.post("/script", data={"navy": "1", "comp": "2"}).status_code == 400
    assert client.post("/script/stream", data={"navy": "1", "comp": "2"}).status_code == 400


def test_an_objection_reply_needs_the_form_token(client):
    data = {"navy": "1", "comp": "2", "preset": "price"}
    assert client.post("/script/objection", data=data).status_code == 400
    assert client.post("/script/objection/stream", data=data).status_code == 400


@pytest.fixture
def offline(monkeypatch, client):
    """The call routes with the index built from test rows and no database or model behind them."""
    import app.call as call
    cat = Catalog.from_rows(_rows(), 1, aliases={"NHB": ["navy", "our"], "NTF": ["teachers"]})
    monkeypatch.setattr(call.cat, "catalog", lambda db: cat)
    monkeypatch.setattr(call, "Snapshot", lambda: None)
    monkeypatch.setattr(call, "get_client", lambda: None)
    monkeypatch.setattr(call.calls, "begin", lambda kind, detail: ({"id": None, "detail": detail, "t0": 0}, None))
    monkeypatch.setattr(call.calls, "end", lambda *a, **kw: None)
    with client.session_transaction() as sess:
        sess["csrf"] = "token"
    return client


def test_the_index_is_compressed_and_revalidated_with_an_etag(offline):
    import gzip
    import json
    r = offline.get("/call/index.json", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers["Content-Encoding"] == "gzip"
    data = json.loads(gzip.decompress(r.data))
    row = next(p for p in data["policies"] if p[0] == 4)
    assert row[1].startswith("HCF CORPORATE") and row[2] == "HCF" and row[3] == "h" and row[4] == "Bronze Plus"
    assert data["funds"]["NHB"]["aliases"] == ["navy", "our"] and data["funds"]["NHB"]["navy"]
    assert data["states"]["melbourne"] == "VIC" and data["needs"]["knees"]["chip"] == "joints"
    again = offline.get("/call/index.json", headers={"If-None-Match": r.headers["ETag"]})
    assert again.status_code == 304


def test_a_follow_up_is_read_against_the_comparison_on_screen(offline, monkeypatch):
    import json

    import app.call as call
    seen = {}

    def fake_plan(text, *, client, model, context=None):
        seen["text"], seen["context"] = text, context
        return planning.Plan(comp_terms=["teachers"], features=["price"], intent="add", confidence=0.9)
    monkeypatch.setattr(call.planning, "plan", fake_plan)
    ctx = {"navy": ["Core Silver+ Hospital"], "competitors": ["Frank Bronze Hospital"], "state": "NSW",
           "household": "couple", "secret": "dropped"}
    r = offline.post("/call/plan", data={"csrf": "token", "request": "add teachers", "context": json.dumps(ctx)})
    d = r.get_json()
    assert r.status_code == 200 and d["understood"] and d["intent"] == "add"
    assert seen["context"] == {k: v for k, v in ctx.items() if k != "secret"}
    assert {p["id"] for p in d["comp"][0]["policies"]} == {6, 7}
    assert d["features"] == [{"key": "price", "label": d["features"][0]["label"]}]


def test_a_broken_context_is_ignored_rather_than_refused(offline, monkeypatch):
    import app.call as call
    seen = {}
    monkeypatch.setattr(call.planning, "plan",
                        lambda text, **kw: seen.update(kw) or planning.Plan(intent="new", confidence=0.9))
    r = offline.post("/call/plan", data={"csrf": "token", "request": "start again", "context": "{not json"})
    assert r.status_code == 200 and seen["context"] is None and r.get_json()["intent"] == "new"


def test_the_planner_sends_the_screen_and_caches_its_instructions():
    sent = {}
    reply = NS(usage=None, content=[NS(type="tool_use", name="plan_request",
                                       input={"intent": "cheaper", "confidence": 0.8})])
    client = NS(messages=NS(create=lambda **kw: sent.update(kw) or reply))
    p = planning.plan("cheaper?", client=client, model="m", context={"navy": ["Core"]})
    assert p.intent == "cheaper"
    assert sent["messages"][0]["content"].startswith('The comparison on screen: {"navy": ["Core"]}')
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    odd = NS(messages=NS(create=lambda **kw: NS(usage=None, content=[
        NS(type="tool_use", name="plan_request", input={"intent": "dance", "confidence": 0.8})])))
    assert planning.plan("x", client=odd, model="m").intent == "compare"


def test_every_clinical_category_has_a_plain_english_definition():
    import app.call as call
    from config.features import CATEGORY_LABELS
    assert set(call.CAT_DEFS) == {f"cover:{k}" for k in CATEGORY_LABELS}
    assert all(d.strip() and d[0].isupper() for d in call.CAT_DEFS.values())


def test_a_clinical_category_difference_shows_what_it_covers_not_a_restated_verdict(client):
    # Rendered directly against the template, with a hand-built difference, so this does not depend on
    # which two policies happen to differ on a category in whichever database is configured.
    import app.call as call
    from engine import claims as cl
    from engine.benefits import Value
    from engine.differences import Card, compare_pair
    navy = Value("cover:BoneJointMuscle", "cover_category", "Covered", 2.0, 2.0)
    comp = Value("cover:BoneJointMuscle", "cover_category", "Not covered", 0.0, 0.0)
    d = compare_pair("cover:BoneJointMuscle", navy, comp)
    c = Card(navy_id=1, comp_id=2, stronger=[d], watch=[], trade_offs=[], same=[], missing=[], price=None)
    hospital = cl.hospital_sheet(1, 2, {1: {d.key: navy}, 2: {d.key: comp}})
    with client.application.app_context():
        from flask import render_template
        html = render_template(
            "_card.html", error=None, heads=[], by_id={1: {"name": "Navy"}, 2: {"name": "Comp", "fund_name": "HCF"}},
            shown=[{"card": c, "answers": [], "extras": [], "neither": [], "hospital": hospital, "price": None,
                    "objections": []}], rows=[],
            state="NSW", family="couple", scope="", anchor=None, chips=set(), show_all=False, wanted=None,
            focus=[], snapshot=1, release_date="2026-01-01", missing_rates=[], chip_rows=set(),
            excess_options=[], followups=[], auto=None, meta={}, comp_only=False, cat_defs=call.CAT_DEFS,
            focus_categories=set(), extras_rows_shown=call.EXTRAS_ROWS_SHOWN,
            hospital_items_shown=call.HOSPITAL_ITEMS_SHOWN)
    assert "Navy Health covers, HCF doesn" in html and "Bone, joint and muscle" in html
    assert "Injuries and diseases of the musculoskeletal system" in html
    # The Covered/Not covered values already say which side has it; nothing should restate that as a
    # separate sentence ("Not covered with HCF: ...").
    assert "Not covered with" not in html
    # ...but it does say what the difference is worth: the typical cost and the public wait.
    assert "typically $" in html and "public patient" in html


def _stub_pack():
    from ai.facts import EvidencePack, Fact
    return EvidencePack.of([Fact.model_validate({
        "fact_id": "F1", "type": "premium", "subject": {}, "value": {"navy": 100, "comp": 120},
        "unit": "AUD/month", "names": ["Core", "HCF"], "scope": {},
        "coverage": {"requested": "price", "complete_for_claim": True},
    })])


def test_an_objection_with_no_preset_and_no_text_is_refused(offline):
    r = offline.post("/script/objection", data={"csrf": "token", "navy": "1", "comp": "2"})
    assert r.status_code == 400 and "say" in r.get_json()["error"].lower()


def test_an_objection_uses_its_own_prompt_and_the_stated_question(offline, monkeypatch):
    import app.call as call
    from ai.compose import ComposeResult
    monkeypatch.setattr(call, "_talk_pack", lambda sel, navy_id, comp_id: (_stub_pack(), None))
    seen = {}

    def fake_compose(pack, *, client, system, question, context, **kw):
        seen["system"], seen["question"], seen["context"] = system, question, context
        return ComposeResult(text="Here is the reply.", outcome="pass")
    monkeypatch.setattr(call, "compose", fake_compose)

    r = offline.post("/script/objection", data={"csrf": "token", "navy": "1", "comp": "2",
                                                 "preset": "price", "state": "NSW", "family": "couple"})
    d = r.get_json()
    assert r.status_code == 200 and d["text"] == "Here is the reply." and d["outcome"] == "pass"
    assert d["question"].startswith("The member says the competitor's policy is cheaper.")
    assert "reply to one specific thing the member just said" in seen["system"]
    assert seen["question"] == d["question"]


def test_an_objection_takes_free_text_over_an_unknown_preset(offline, monkeypatch):
    import app.call as call
    from ai.compose import ComposeResult
    monkeypatch.setattr(call, "_talk_pack", lambda sel, navy_id, comp_id: (_stub_pack(), None))
    seen = {}

    def fake_compose(pack, *, question, **kw):
        seen["q"] = question
        return ComposeResult(text="x", outcome="pass")
    monkeypatch.setattr(call, "compose", fake_compose)
    r = offline.post("/script/objection", data={"csrf": "token", "navy": "1", "comp": "2",
                                                 "objection": "  they said it sounds complicated  "})
    assert r.status_code == 200 and seen["q"] == "they said it sounds complicated"


def test_an_objection_is_capped_in_length(offline, monkeypatch):
    import app.call as call
    from ai.compose import ComposeResult
    monkeypatch.setattr(call, "_talk_pack", lambda sel, navy_id, comp_id: (_stub_pack(), None))
    seen = {}

    def fake_compose(pack, *, question, **kw):
        seen["q"] = question
        return ComposeResult(text="x", outcome="pass")
    monkeypatch.setattr(call, "compose", fake_compose)
    long_text = "a" * 500
    offline.post("/script/objection", data={"csrf": "token", "navy": "1", "comp": "2", "objection": long_text})
    assert len(seen["q"]) == call.MAX_OBJECTION_CHARS


def _estimate_data():
    """What engine/benefits.fetch returns, for one Navy Health and one competitor extras policy."""
    def rate(pid, premium):
        return {"policy_id": pid, "excess_type": None, "excess": 0, "per_person": None, "per_policy": None,
                "per_admission": None, "premium": premium, "copayment_type": None, "copay_shared": None,
                "copay_private": None, "copay_day_surgery": None, "ambulance": "None"}

    def limit(pid, per_person):
        return {"policy_id": pid, "title": "physio", "no_limit": False, "per_person": per_person, "per_service": None,
                "per_policy": None, "lifetime": None, "count": None, "count_years": None, "count_type": None,
                "free_text": None, "services": ["Physiotherapy"]}
    return {
        "snapshot": 1, "release_date": "2026-10-01", "closest": [], "profiles": [], "cover": [],
        "policies": [{"id": 1, "name": "Navy Extras", "product_type": "extras", "is_navy": True, "fund_code": "NHB",
                      "fund_name": "Navy Health", "status": "confirmed"},
                     {"id": 2, "name": "Their Extras", "product_type": "extras", "is_navy": False, "fund_code": "HCF",
                      "fund_name": "HCF", "status": "confirmed"}],
        "rates": [rate(1, 50.0), rate(2, 60.0)],
        "extras": [{"policy_id": p, "service": "Physiotherapy", "covered": True, "wait_months": 2} for p in (1, 2)],
        "benefits": [{"policy_id": 1, "service": "Physiotherapy", "item": "PhysiotherapySubsequent", "amount": 50, "pct": None},
                     {"policy_id": 2, "service": "Physiotherapy", "item": "PhysiotherapySubsequent", "amount": 40, "pct": None}],
        "limits": [limit(1, 800), limit(2, 300)],
    }


def test_the_estimate_prices_a_usage_profile_on_both_policies_with_premiums_beside_it(offline, monkeypatch):
    import json

    import app.call as call
    monkeypatch.setattr(call.bn, "fetch", lambda db, **kw: _estimate_data())
    uses = [{"item": "PhysiotherapySubsequent", "times": 10, "people": 1, "fee": 95}]
    r = offline.post("/call/estimate", data={"csrf": "token", "navy": "1", "comp": "2", "state": "NSW",
                                             "family": "couple", "uses": json.dumps(uses)})
    d = r.get_json()
    assert r.status_code == 200
    assert d["navy"]["total"] == 500 and d["comp"]["total"] == 300       # 10 x $50; 10 x $40 capped at $300
    assert d["navy"]["premium_year"] == 600 and d["comp"]["premium_year"] == 720
    assert d["navy"]["fund"] == "Navy Health" and d["comp"]["fund"] == "HCF"
    assert "annual limit" in d["comp"]["lines"][0]["note"] and "2 month wait" in d["navy"]["lines"][0]["note"]


def test_the_estimate_rejects_a_usage_list_it_cannot_read(offline):
    r = offline.post("/call/estimate", data={"csrf": "token", "navy": "1", "comp": "2", "uses": "{nope"})
    assert r.status_code == 400


def test_usage_is_read_by_the_model_into_lines_the_rep_can_edit(offline, monkeypatch):
    import app.call as call
    from ai.usage import UsageRead
    from engine.estimate import Use
    seen = {}

    def fake_read(text, *, client, model, household):
        seen["text"], seen["household"] = text, household
        return UsageRead(uses=[Use("PhysiotherapySubsequent", 52, 1)], unclear=["a GP visit"])
    monkeypatch.setattr(call, "read_usage", fake_read)
    r = offline.post("/call/usage", data={"csrf": "token", "text": "weekly physio and a GP visit", "family": "couple"})
    d = r.get_json()
    assert r.status_code == 200
    assert d["uses"] == [{"item": "PhysiotherapySubsequent", "times": 52, "people": 1}]
    assert d["unclear"] == ["a GP visit"] and seen["household"] == "couple"


def test_usage_needs_something_to_read_and_the_form_token(offline):
    assert offline.post("/call/usage", data={"csrf": "token", "text": "  "}).status_code == 400
    assert offline.post("/call/usage", data={"csrf": "token", "text": "x" * 401}).status_code == 400
    assert offline.post("/call/usage", data={"csrf": "wrong", "text": "physio"}).status_code in (400, 403)


@needs_db
def test_the_estimate_works_on_a_real_pair(client):
    import json
    with client.session_transaction() as sess:
        sess["csrf"] = "token"
    uses = [{"item": "DentalGeneral012PeriodicExam", "times": 2, "people": 2},
            {"item": "OpticalSingleVisionLenses", "times": 1, "people": 1}]
    r = client.post("/call/estimate", data={"csrf": "token", "navy": "2789", "comp": "115", "state": "NSW",
                                            "family": "couple", "uses": json.dumps(uses)})
    d = r.get_json()
    assert r.status_code == 200 and len(d["navy"]["lines"]) == 2
    assert d["navy"]["total"] >= 0 and d["comp"]["total"] >= 0
