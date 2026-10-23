"""From a rep's request to the policies it could mean, offered back for the rep to confirm.

    request -> plan (a model reads the words)  ->  candidates (the database finds policies)  ->  the rep ticks

The model decides only what the rep is asking about: which terms name policies, which features matter,
and whether all of a group is meant. Finding the policies, and every figure after that, is done by the
database. A request the model cannot read gives an empty plan, and the page asks the rep to type a term.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.reference import CLINICAL_CATEGORIES, EXTRAS_SERVICES, FAMILY_TYPES, STATES
from engine import search
from engine.db import Snapshot

PLANNER_PROMPT = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "planner_v1.md"
PRODUCT_TYPES = ("hospital", "extras", "combined")
MAX_TERMS = 6

PLAN_TOOL = {
    "name": "plan_request",
    "description": "Turn the rep's request into what to look up. Use the exact enumerated values only.",
    "input_schema": {
        "type": "object",
        "properties": {
            "navy_terms": {"type": "array", "items": {"type": "string"}},
            "navy_all": {"type": "boolean"},
            "comp_terms": {"type": "array", "items": {"type": "string"}},
            "comp_all": {"type": "boolean"},
            "features": {"type": "array", "items": {"type": "string",
                                                    "enum": list(CLINICAL_CATEGORIES) + list(EXTRAS_SERVICES)}},
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
    comp_all: bool = False
    features: list[str] = field(default_factory=list)
    product_type: str = ""
    state: str = ""
    family_type: str = ""
    confidence: float = 0.0
    usage: Any = None


@dataclass
class Group:
    label: str
    policies: list[dict]
    total: int
    reason: str


def _clean_terms(values: Any) -> list[str]:
    out = []
    for v in values or []:
        if isinstance(v, str) and v.strip():
            out.append(v.strip()[:80])
    return out[:MAX_TERMS]


def plan(request: str, *, client: Any, model: str) -> Plan:
    """One tool-forced model call. A reply that is not a usable plan is an empty plan, never a guess."""
    system = PLANNER_PROMPT.read_text(encoding="utf-8")
    response = client.messages.create(
        model=model, max_tokens=500, system=system,
        tools=[PLAN_TOOL], tool_choice={"type": "tool", "name": "plan_request"},
        messages=[{"role": "user", "content": request}])
    usage = getattr(response, "usage", None)
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "plan_request":
            d = dict(block.input)
            features = [f for f in d.get("features") or [] if f in CLINICAL_CATEGORIES or f in EXTRAS_SERVICES]
            return Plan(
                navy_terms=_clean_terms(d.get("navy_terms")), navy_all=bool(d.get("navy_all")),
                comp_terms=_clean_terms(d.get("comp_terms")), comp_all=bool(d.get("comp_all")),
                features=features,
                product_type=d.get("product_type") if d.get("product_type") in PRODUCT_TYPES else "",
                state=d.get("state") if d.get("state") in STATES else "",
                family_type=d.get("family_type") if d.get("family_type") in FAMILY_TYPES else "",
                confidence=float(d.get("confidence") or 0), usage=usage)
    return Plan(usage=usage)


def candidates(db: Snapshot, p: Plan) -> list[Group]:
    """The policies the plan could mean, as groups for the rep to tick. Each term contributes the policies
    it matches; a feature narrows a group to the policies that cover it in the chosen state."""
    groups: list[Group] = []
    for navy in (True, False):
        terms = p.navy_terms if navy else p.comp_terms
        select_all = p.navy_all if navy else p.comp_all
        label = "Navy Health" if navy else "Competitors"
        seen: dict[int, dict] = {}
        reasons: list[str] = []
        if select_all:
            found, total = search.find(db, navy=navy, product_type=p.product_type)
            for r in found:
                seen[r["id"]] = r
            reasons.append("all of them")
        else:
            for term in terms:
                found, total = search.find(db, text=term, navy=navy, product_type=p.product_type)
                for r in found:
                    seen.setdefault(r["id"], r)
                reasons.append(f"matches '{term}'")
        if p.features and seen:
            if not p.state:
                reasons.append("covering the features asked about (needs a state)")
            else:
                clinical = [f for f in p.features if f in CLINICAL_CATEGORIES]
                extras = [f for f in p.features if f in EXTRAS_SERVICES]
                keep = set(seen)
                if clinical:
                    keep &= {r["id"] for r in search.feature_match(db, categories=clinical, state=p.state, navy=navy)}
                if extras:
                    keep &= {r["id"] for r in search.extras_match(
                        db, services=extras, state=p.state, family=p.family_type or "couple", navy=navy)}
                if keep:
                    seen = {k: v for k, v in seen.items() if k in keep}
                    reasons.append(f"covering {', '.join(p.features)} in {p.state}")
                else:
                    # Nothing covers all of it. Say so, and still offer the matches, so the rep can
                    # choose to compare them anyway rather than being shown nothing.
                    reasons.append(f"none cover {', '.join(p.features)} in {p.state}; showing all matches")
        if seen or terms or select_all:
            ordered = sorted(seen.values(), key=lambda r: (not r["is_navy"], r["current_name"]))
            groups.append(Group(label=label, policies=ordered, total=len(ordered),
                                reason="; ".join(reasons)))
    return groups
