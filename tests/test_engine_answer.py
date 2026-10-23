"""The question path: routing, name resolution, clarifying questions, and the household defaults.

The model is replaced by a scripted route and the writer by a stub, so these tests show
what the rep is shown for each routed question without calling a model. The facts come from
the live snapshot of the local database when one is available (see the skip below).
"""
import os
from types import SimpleNamespace as NS

import pytest

from engine import answer as a
from engine.db import Snapshot


def _db_available() -> bool:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return False
    try:
        return Snapshot().live_snapshot_id() > 0
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="needs DATABASE_URL pointing at a loaded snapshot")


class ScriptedClient:
    """A stand-in for the Anthropic client: returns one tool call with the given input."""

    def __init__(self, route_input: dict):
        self.route_input = route_input
        self.messages = self

    def create(self, **kwargs):
        return NS(content=[NS(type="tool_use", name="route_question", input=self.route_input)])


class StubWriter:
    def __init__(self):
        self.packs = []

    def __call__(self, pack, **kwargs):
        self.packs.append(pack)
        return NS(text="written", outcome="pass", usage=None)


def _ask(route_input: dict, db, writer=None):
    writer = writer or StubWriter()
    return a.answer("question", db=db, client=ScriptedClient(route_input), model="m", compose=writer), writer


def test_an_unknown_question_is_declined_without_a_database_call():
    class NoDb:
        def rows(self, *args, **kwargs):
            raise AssertionError("a declined question must not touch the database")

    result, writer = _ask({"question_type": "none", "confidence": 0.9}, NoDb())
    assert result.outcome == "declined" and result.text == a.CANNOT_ANSWER
    assert writer.packs == []


def test_low_confidence_is_declined_even_when_a_type_is_given():
    class NoDb:
        def rows(self, *args, **kwargs):
            raise AssertionError("a low-confidence route must not touch the database")

    result, _ = _ask({"question_type": "price", "confidence": 0.3}, NoDb())
    assert result.outcome == "declined"


def test_a_garbled_route_is_none_not_a_guess():
    client = NS(messages=NS(create=lambda **kw: NS(content=[NS(type="text", text="no idea")])))
    routed = a.route("anything", client=client, model="m")
    assert routed.question_type == "none" and routed.confidence == 0.0


@needs_db
def test_a_competitor_name_that_is_not_found_asks_rather_than_guessing():
    db = Snapshot()
    result, writer = _ask({"question_type": "head_to_head", "navy_policy": "Core Silver+ Hospital",
                           "comp_policy": "No Such Fund Policy Gold", "state": "NSW", "confidence": 0.9}, db)
    assert result.outcome == "clarify"
    assert "competitor" in result.text
    assert writer.packs == []


@needs_db
def test_an_ambiguous_navy_name_lists_the_candidates():
    db = Snapshot()
    result, writer = _ask({"question_type": "price", "navy_policy": "Premium Gold Hospital", "state": "QLD",
                           "confidence": 0.9}, db)
    assert result.outcome == "clarify"
    assert "Premium Gold Hospital 350" in result.text and "Premium Gold Hospital 750" in result.text
    assert writer.packs == []


@needs_db
def test_an_exact_name_resolves_and_the_household_default_is_disclosed():
    db = Snapshot()
    result, writer = _ask({"question_type": "best_fit",
                           "comp_policy": "HCF CORPORATE CARE BRONZE PLUS HOSPITAL $750 EXCESS",
                           "state": "NSW", "confidence": 0.9}, db)
    assert result.outcome == "pass"
    assert any("couple household" in d for d in result.disclosures)
    assert writer.packs, "the facts should have reached the writer"
    assert all(f.subject.get("comp_policy_id") for f in writer.packs[0].facts.values())


@needs_db
def test_closed_products_resolve_without_a_state():
    db = Snapshot()
    result, writer = _ask({"question_type": "closed_product", "navy_policy": "Premium Gold Hospital 500",
                           "confidence": 0.9}, db)
    assert result.outcome == "pass"
    assert writer.packs and any(f.fact_id.startswith("status.") for f in writer.packs[0].facts.values())


@needs_db
def test_a_question_without_a_state_asks_for_one():
    db = Snapshot()
    result, writer = _ask({"question_type": "is_covered", "navy_policy": "Premium Gold Hospital 350",
                           "clinical_category": "JointReplacements", "confidence": 0.9}, db)
    assert result.outcome == "clarify" and "state" in result.text
    assert writer.packs == []


def test_names_are_never_resolved_across_navy_and_competitors_by_accident(monkeypatch):
    """A competitor name must not come back as a Navy policy: the navy flag is part of the lookup."""
    seen = []

    class Db:
        def rows(self, sql, params=None):
            seen.append(params["navy"])
            return []

    assert a.find_policy(Db(), "anything", navy=True) == (None, [])
    assert a.find_policy(Db(), "anything", navy=False) == (None, [])
    assert seen == [True, False]
