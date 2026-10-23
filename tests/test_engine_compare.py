"""The comparison table, the policy search, and the candidate groups the rep confirms.

These run against a loaded snapshot (skipped without one). The request-reading model is never called:
the plans are written out directly, as the router's output would be.
"""
import pytest

from engine import plan as planning
from engine import search
from engine.compare import NO_RATE, compare_many, order
from engine.db import EngineError, Snapshot
from tests.test_engine_answer import needs_db


def _policies(db, ids):
    return db.rows("SELECT p.id, p.current_name, p.product_type, p.is_navy, f.name AS fund_name "
                   "FROM policies p JOIN funds f ON f.fund_code = p.fund_code "
                   "WHERE p.id = ANY(ARRAY[" + ",".join(str(i) for i in ids) + "]::bigint[])")


def test_navy_policies_come_first_whatever_order_they_were_given():
    rows = [{"id": 1, "is_navy": False}, {"id": 2, "is_navy": True}, {"id": 3, "is_navy": False},
            {"id": 4, "is_navy": True}]
    assert [r["id"] for r in order(rows)] == [2, 4, 1, 3]


def test_no_policies_is_an_error_not_an_empty_table():
    with pytest.raises(EngineError):
        compare_many(None, policies=[], state="NSW", family="couple", scope="")


@needs_db
def test_three_policies_give_three_columns_with_navy_first():
    db = Snapshot()
    table = compare_many(db, policies=_policies(db, [115, 2330, 2789]), state="NSW", family="couple", scope="")
    names = [c["name"] for c in table["columns"]]
    assert [c["is_navy"] for c in table["columns"]] == [True, True, False]
    assert names[-1].startswith("HCF")
    cats = next(s for s in table["sections"] if s["title"] == "Clinical categories")
    assert all(len(r["cells"]) == 3 for r in cats["rows"])
    assert len(cats["rows"]) == 38


@needs_db
def test_agreement_flags_follow_the_values():
    db = Snapshot()
    table = compare_many(db, policies=_policies(db, [115, 2330, 2789]), state="NSW", family="couple", scope="")
    for section in table["sections"]:
        for row in section["rows"]:
            assert row["same"] == (len(set(row["cells"])) == 1), row


@needs_db
def test_premiums_are_not_set_against_a_different_excess():
    db = Snapshot()
    table = compare_many(db, policies=_policies(db, [2789, 115]), state="NSW", family="couple", scope="")
    price = next(s for s in table["sections"] if s["title"].startswith("Price"))
    by_label = {r["feature"]: r["cells"] for r in price["rows"]}
    assert by_label["Premium a month, $500 excess"] == ["$384.77", NO_RATE]
    assert by_label["Premium a month, $750 excess"] == [NO_RATE, "$278.08"]


@needs_db
def test_the_search_needs_every_word_and_finds_the_navy_policy_by_a_partial_name():
    db = Snapshot()
    found, total = search.find(db, text="gold 350", navy=True)
    assert total >= 1 and all("350" in r["current_name"] for r in found[:1])
    none, total = search.find(db, text="gold zzzz", navy=True)
    assert total == 0


@needs_db
def test_a_feature_narrows_the_candidates_to_policies_that_cover_it():
    db = Snapshot()
    everything, all_total = search.find(db, navy=True)
    covering = search.feature_match(db, categories=["JointReplacements"], state="NSW", navy=True)
    assert covering and len(covering) <= all_total
    assert all(c["is_navy"] for c in covering)


@needs_db
def test_candidate_groups_list_navy_and_competitors_separately_for_a_plan():
    db = Snapshot()
    p = planning.Plan(navy_terms=["gold 350"], comp_terms=["hcf bronze"], state="NSW", family_type="couple")
    groups = planning.candidates(db, p)
    assert [g.label for g in groups] == ["Navy Health", "Competitors"]
    assert all(r["is_navy"] for r in groups[0].policies)
    assert not any(r["is_navy"] for r in groups[1].policies)
    assert groups[1].total >= 1


@needs_db
def test_a_feature_with_no_state_is_not_silently_applied():
    db = Snapshot()
    p = planning.Plan(navy_all=True, features=["JointReplacements"], state="")
    groups = planning.candidates(db, p)
    assert "needs a state" in groups[0].reason
    assert groups[0].total > 0


def test_a_plan_reply_with_unknown_values_drops_them():
    from types import SimpleNamespace as NS

    class Client:
        messages = None

    class Msg:
        def create(self, **kw):
            return NS(usage=None, content=[NS(type="tool_use", name="plan_request", input={
                "navy_terms": ["gold"], "features": ["JointReplacements", "Teleportation"],
                "state": "XX", "family_type": "couple", "product_type": "dental", "confidence": 0.9})])

    client = NS(messages=Msg())
    p = planning.plan("q", client=client, model="m")
    assert p.features == ["JointReplacements"]
    assert p.state == "" and p.product_type == "" and p.family_type == "couple"
    assert p.navy_terms == ["gold"]
