"""A rep's question, from text to the sentences they may read.

    route  ->  resolve the names  ->  dispatch to the question type  ->  facts  ->  write and verify

Navy Health is always the base. The navy side of any comparison is a Navy
Health policy; the comp side is a competitor's. A question that names only a
competitor is answered for the competitor, or as a best fit for Navy Health's
products, and never as a Navy Health figure.

Nothing here answers from memory. A question the types do not cover returns
the fixed "I can't answer that" line, and so does a question whose policies
cannot be found.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ai.facts import EvidencePack
from config.reference import CLINICAL_CATEGORIES, DEPENDANT_TITLES, EXTRAS_ITEMS, FAMILY_TYPES, STATES
from engine import queries as q
from engine.db import EngineError, Snapshot

ROUTER_PROMPT = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "router_v1.md"
CANNOT_ANSWER = "I can't answer that from the policy data."
QUESTION_TYPES = ("head_to_head", "price", "best_fit", "is_covered", "waiting_switch", "accommodation",
                  "excess_options", "ambulance", "extras_item", "closed_product", "none")

# Household defaults, applied and disclosed when the question does not say. The writer must say so.
DEFAULT_FAMILY = "couple"
DEFAULT_SCOPE = ""

ROUTE_TOOL = {
    "name": "route_question",
    "description": "Route the rep's question to one question type and the slots it names.",
    "input_schema": {
        "type": "object",
        "properties": {
            "question_type": {"type": "string", "enum": list(QUESTION_TYPES)},
            "navy_policy": {"type": "string"},
            "comp_policy": {"type": "string"},
            "state": {"type": "string", "enum": [s for s in STATES if s != "ALL"] + [""]},
            "family_type": {"type": "string", "enum": list(FAMILY_TYPES) + [""]},
            "dependant_titles": {"type": "array", "items": {"type": "string", "enum": list(DEPENDANT_TITLES)}},
            "clinical_category": {"type": "string", "enum": list(CLINICAL_CATEGORIES) + [""]},
            "extras_item": {"type": "string", "enum": list(EXTRAS_ITEMS) + [""]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["question_type", "confidence"],
    },
}


@dataclass
class Routed:
    question_type: str
    confidence: float
    slots: dict[str, Any] = field(default_factory=dict)


@dataclass
class Answer:
    text: str
    question_type: str
    outcome: str
    unresolved: list[str] = field(default_factory=list)
    disclosures: list[str] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)


def route(question: str, *, client: Any, model: str) -> Routed:
    """One tool-forced model call. A reply that is not a valid route is 'none', never a guess."""
    system = ROUTER_PROMPT.read_text(encoding="utf-8")
    response = client.messages.create(
        model=model, max_tokens=400, system=system,
        tools=[ROUTE_TOOL], tool_choice={"type": "tool", "name": "route_question"},
        messages=[{"role": "user", "content": question}])
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "route_question":
            data = dict(block.input)
            qt = data.get("question_type", "none")
            if qt not in QUESTION_TYPES:
                qt = "none"
            conf = float(data.get("confidence", 0))
            return Routed(question_type=qt, confidence=conf, slots={k: v for k, v in data.items()
                                                                    if k not in ("question_type", "confidence")})
    return Routed(question_type="none", confidence=0.0)


# ── names: the policy the rep meant, and only when it is clear ────────────────────

_FIND = """
SELECT p.id, p.current_name, p.product_type, p.is_navy, f.name AS fund_name,
       similarity(lower(p.current_name), lower(:name)) AS score
FROM policies p JOIN funds f ON f.fund_code = p.fund_code
WHERE (:include_closed OR p.status <> 'closed') AND p.is_navy = :navy
ORDER BY score DESC, p.current_name
LIMIT 6
"""


def find_policy(db: Snapshot, name: str, *, navy: bool, include_closed: bool = False) -> tuple[Optional[dict], list[dict]]:
    """The one policy a name means, or None with the close candidates. A name is resolved only when the
    best match is clearly better than the next: a rep should never be shown a figure for the wrong
    product because two names were close."""
    if not name.strip():
        return None, []
    cands = db.rows(_FIND, {"name": name.strip(), "navy": navy, "include_closed": include_closed})
    if not cands:
        return None, []
    exact = [c for c in cands if c["current_name"].strip().lower() == name.strip().lower()]
    if len(exact) == 1:
        return exact[0], cands
    top = cands[0]
    second = cands[1]["score"] if len(cands) > 1 else 0
    if float(top["score"]) >= 0.6 and float(top["score"]) - float(second) >= 0.15:
        return top, cands
    return None, cands


# ── dispatch: a routed question to the facts that answer it ─────────────────────

def _need(name: Optional[str], what: str) -> str:
    if not name:
        raise EngineError(what)
    return name


def dispatch(db: Snapshot, routed: Routed, navy: Optional[dict], comp: Optional[dict],
             household: dict) -> list[dict]:
    """The engine call for the routed type. Raises EngineError when a required slot is missing, which
    the caller turns into a plain question back to the rep."""
    s = routed.slots
    qt = routed.question_type
    state = household["state"]
    family = household["family_type"]
    scope = household["dependant_scope"]
    if qt == "head_to_head":
        return q.head_to_head(db, navy_policy_id=_need(navy and navy["id"], "navy policy"),
                              comp_policy_id=_need(comp and comp["id"], "competitor policy"),
                              state=state, family=family, scope=scope)
    if qt == "price":
        return q.price(db, navy_policy_id=_need(navy and navy["id"], "navy policy"),
                       comp_policy_id=comp and comp["id"], state=state, family=family, scope=scope)
    if qt == "best_fit":
        return q.best_fit(db, comp_policy_id=_need(comp and comp["id"], "competitor policy"),
                          state=state, family=family, scope=scope)
    if qt == "is_covered":
        policy = _need(navy or comp, "policy")
        return q.is_covered(db, policy_id=policy["id"], category=_need(s.get("clinical_category") or None,
                                                                      "clinical category"), state=state)
    if qt == "waiting_switch":
        out = q.waiting(db, policy_id=_need(navy and navy["id"], "navy policy"), state=state)
        if comp:
            out += q.waiting(db, policy_id=comp["id"], state=state)
        return out
    if qt == "accommodation":
        policy = _need(navy or comp, "policy")
        return q.accommodation(db, policy_id=policy["id"], state=state)
    if qt == "excess_options":
        policy = _need(navy or comp, "policy")
        return q.excess_options(db, policy_id=policy["id"], state=state, family=family, scope=scope)
    if qt == "ambulance":
        policy = _need(navy or comp, "policy")
        return q.ambulance(db, policy_id=policy["id"], state=state)
    if qt == "extras_item":
        policy = _need(navy or comp, "policy")
        return q.extras_item(db, policy_id=policy["id"], item=_need(s.get("extras_item") or None, "extras item"),
                             state=state, family=family)
    if qt == "closed_product":
        policy = _need(navy or comp, "policy")
        return q.closed_product(db, policy_id=policy["id"])
    raise EngineError("no answer for this question type")


# ── the whole path ──────────────────────────────────────────────────────────────

def answer(question: str, *, db: Snapshot, client: Any, model: str,
           compose: Callable[..., Any], context: Optional[dict] = None) -> Answer:
    """Route, resolve, compute the facts, and have them written and verified. `compose` is
    ai.compose.compose, passed in so the call can be tested without a model."""
    routed = route(question, client=client, model=model)
    if routed.question_type == "none" or routed.confidence < 0.6:
        return Answer(text=CANNOT_ANSWER, question_type="none", outcome="declined")

    unresolved: list[str] = []
    # A closed product is still a thing a rep asks about ("is it still sold?"), so only that type searches closed ones.
    closed_ok = routed.question_type == "closed_product"
    navy, navy_cands = find_policy(db, routed.slots.get("navy_policy", ""), navy=True, include_closed=closed_ok)
    comp, comp_cands = find_policy(db, routed.slots.get("comp_policy", ""), navy=False, include_closed=closed_ok)
    if routed.slots.get("navy_policy") and navy is None:
        unresolved.append(f"which Navy Health policy: {', '.join(c['current_name'] for c in navy_cands[:3])}")
    if routed.slots.get("comp_policy") and comp is None:
        unresolved.append("which competitor policy was meant")
    if unresolved:
        return Answer(text="I need to know: " + "; ".join(unresolved) + ".", question_type=routed.question_type,
                      outcome="clarify", unresolved=unresolved)

    disclosures: list[str] = []
    state = routed.slots.get("state") or ""
    if not state and routed.question_type != "closed_product":
        return Answer(text="Which state? Premiums and cover differ by state.", question_type=routed.question_type,
                      outcome="clarify", unresolved=["the state"])
    family = routed.slots.get("family_type") or DEFAULT_FAMILY
    if not routed.slots.get("family_type") and routed.question_type in ("price", "head_to_head", "best_fit", "excess_options"):
        disclosures.append(f"Assumed a {family} household; the premium is for that household.")
    titles = sorted(set(routed.slots.get("dependant_titles") or []))
    scope = "+".join(titles)
    household = {"state": state, "family_type": family, "dependant_scope": scope}

    try:
        facts = dispatch(db, routed, navy, comp, household)
    except EngineError as exc:
        return Answer(text=f"I need one more detail: {exc}.", question_type=routed.question_type,
                      outcome="clarify", unresolved=[str(exc)])

    if not facts:
        return Answer(text=CANNOT_ANSWER, question_type=routed.question_type, outcome="declined", facts=[])

    pack = EvidencePack.of(_validated(facts), name=routed.question_type)
    result = compose(pack, client=client, system=ROUTER_PROMPT.parent.joinpath("writer_v1.md").read_text(encoding="utf-8"),
                     question=question, context={**(context or {}), "disclosures": disclosures})
    text = result.text or CANNOT_ANSWER
    return Answer(text=text, question_type=routed.question_type, outcome=result.outcome,
                  disclosures=disclosures, facts=facts)


def _validated(facts: list[dict]) -> list:
    from ai.facts import Fact
    return [Fact.model_validate(f) for f in facts]
