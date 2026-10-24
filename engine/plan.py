"""From a rep's request in plain words to the policies and benefits it means, offered back to confirm.

    request  ->  plan (one model call reads the words)  ->  suggestions (the index finds policies)

The model only decides what the rep is asking about: the words that name policies or companies, the
benefits they care about, and the state and household if they said them. Finding the policies is the
type-ahead index's job (engine/catalog.py), and the rep confirms every policy before anything is
compared: a term that matches one policy exactly is ticked for them, anything else is offered as a
choice. A request the model cannot read gives an empty plan, never a guess.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.features import FEATURES
from config.reference import FAMILY_TYPES, STATES
from engine.catalog import Catalog, words

PLANNER_PROMPT = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "planner_v2.md"
PRODUCT_TYPES = ("hospital", "extras", "combined")
MAX_TERMS = 6
MATCHES_PER_TERM = 6

PLAN_TOOL = {
    "name": "plan_request",
    "description": "Turn the rep's request into what to look up. Use the exact enumerated values only.",
    "input_schema": {
        "type": "object",
        "properties": {
            "navy_terms": {"type": "array", "items": {"type": "string"},
                           "description": "Words naming Navy Health policies, as the rep said them"},
            "navy_all": {"type": "boolean", "description": "The rep means all of Navy Health's policies"},
            "comp_terms": {"type": "array", "items": {"type": "string"},
                           "description": "Words naming competitor policies or companies, as the rep said them"},
            "features": {"type": "array", "items": {"type": "string", "enum": list(FEATURES)},
                         "description": "The benefits the rep wants compared"},
            "product_type": {"type": "string", "enum": list(PRODUCT_TYPES) + [""]},
            "state": {"type": "string", "enum": [s for s in STATES if s != "ALL"] + [""]},
            "family_type": {"type": "string", "enum": list(FAMILY_TYPES) + [""]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["confidence"],
    },
}


@dataclass
class Plan:
    navy_terms: list[str] = field(default_factory=list)
    navy_all: bool = False
    comp_terms: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    product_type: str = ""
    state: str = ""
    family_type: str = ""
    confidence: float = 0.0
    usage: Any = None


def _terms(values: Any) -> list[str]:
    return [v.strip()[:80] for v in (values or []) if isinstance(v, str) and v.strip()][:MAX_TERMS]


def plan(request: str, *, client: Any, model: str) -> Plan:
    """One tool-forced model call. Anything the reply holds that is not an allowed value is dropped."""
    response = client.messages.create(
        model=model, max_tokens=600, system=PLANNER_PROMPT.read_text(encoding="utf-8"),
        tools=[PLAN_TOOL], tool_choice={"type": "tool", "name": "plan_request"},
        messages=[{"role": "user", "content": request}])
    usage = getattr(response, "usage", None)
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "plan_request":
            d = dict(block.input)
            return Plan(
                navy_terms=_terms(d.get("navy_terms")), navy_all=bool(d.get("navy_all")),
                comp_terms=_terms(d.get("comp_terms")),
                features=[f for f in d.get("features") or [] if f in FEATURES],
                product_type=d.get("product_type") if d.get("product_type") in PRODUCT_TYPES else "",
                state=d.get("state") if d.get("state") in STATES and d.get("state") != "ALL" else "",
                family_type=d.get("family_type") if d.get("family_type") in FAMILY_TYPES else "",
                confidence=float(d.get("confidence") or 0), usage=usage)
    return Plan(usage=usage)


def _exact(term: str, policies: list[dict]) -> list[dict]:
    t = " ".join(words(term))
    return [p for p in policies if " ".join(words(p["name"])) == t]


def suggestions(cat: Catalog, p: Plan) -> dict:
    """What to offer the rep: per term, the policies and companies it matches, and which (if any) can be
    ticked for them because the term names exactly one policy."""
    out: dict[str, list[dict]] = {"navy": [], "comp": []}
    if p.navy_all:
        found = cat.search("", side="navy", product_type=p.product_type, limit=60)
        out["navy"].append({"term": "all Navy Health policies", "policies": found["policies"], "funds": [],
                            "total": found["total"], "pick": []})
    for side, terms in (("navy", p.navy_terms), ("comp", p.comp_terms)):
        for term in terms:
            found = cat.search(term, side=side, product_type=p.product_type, limit=MATCHES_PER_TERM)
            if not found["policies"] and p.product_type:
                found = cat.search(term, side=side, limit=MATCHES_PER_TERM)
            exact = _exact(term, found["policies"])
            pick = [exact[0]["id"]] if len(exact) == 1 else (
                [found["policies"][0]["id"]] if found["total"] == 1 else [])
            out[side].append({"term": term, "policies": found["policies"], "funds": found["funds"],
                              "total": found["total"], "pick": pick})
    return out
