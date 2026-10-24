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
    assert "Where Navy Health is better" in html and "Back, neck and spine" in html
    assert "$278.08 a month" in html and "at $750 excess" in html


@needs_db
def test_a_competitor_alone_is_compared_with_its_closest_navy_policy(client):
    import json
    import re
    html = client.get("/card?ids=115&state=NSW&family=couple").data.decode()
    meta = json.loads(re.search(r'<script type="application/json" id="card-meta">(.*?)</script>', html, re.S).group(1))
    assert meta["auto_navy"] and meta["auto_navy"]["id"] in meta["columns"]
    assert "best match" in html and 'class="verdict"' in html
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
