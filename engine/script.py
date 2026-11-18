"""The talking point under a comparison card: the card's own differences as Facts for the writer.

The writer (ai/compose.py, prompt ai/prompts/writer_v1.md) may only use these facts, and the verifier
checks every figure and name it writes against them. Each fact holds both sides, `value.navy` and
`value.comp`, so a Navy Health figure can never be stated as the competitor's. What goes in: the premium
line, the top five Navy Health strengths, the top three for the competitor, the excess when it differs,
and anything beyond that which matches what the member said they care about (`chip_match`, set by the
card from the rep's active chips) — a fact can matter to this one call without being the single highest
scoring difference in general, and the writer is told which facts that is true for (`scope`) so it can
lead with relevance to the actual person on the phone, not materiality alone.
"""
from __future__ import annotations

from typing import Optional

from engine.benefits import UNLIMITED, Value
from engine.claims import EVERYDAY_SERVICES, category_costs
from engine.differences import Card, Difference

UNITS = {"premium": "AUD/month", "wait_months": "months", "extras_item": "AUD", "extras_limit": "AUD",
         "excess": "AUD", "copayment": "AUD/day", "tier": "rank"}


def _side(v: Optional[Value]):
    if v is None:
        return None
    if v.size is not None and v.size != UNLIMITED and v.kind in UNITS and v.basis != "percent":
        return v.size
    return v.display


def _fact(fid: str, d: Difference, navy_name: str, comp_name: str, snapshot: int, state: str, family: str,
          costs: Optional[dict] = None) -> dict:
    claimable = {}
    if d.navy and d.navy.note:
        claimable["navy_note"] = d.navy.note
    if d.comp and d.comp.note:
        claimable["comp_note"] = d.comp.note
    # A covered service's wait is part of its display ("Covered, 2 month wait"); without its own field
    # the writer could never bind the 2, and the checker rejected every sentence that mentioned it.
    for side, v in (("navy", d.navy), ("comp", d.comp)):
        if v is not None and v.wait:
            claimable[f"{side}_wait_months"] = v.wait
    # A clinical category says only covered or not; what that is worth is the typical private cost of
    # its most common procedure and the public wait for it (config/category_costs.yaml), given to the
    # writer as figures it can bind so it can say "a knee replacement typically costs about $X".
    procedure, cost_words = "", ""
    if d.kind == "cover_category":
        cost = (costs or {}).get(d.key) or {}
        procedure = cost.get("procedure") or ""
        cost_words = cost.get("cost_words") or "as a private patient"
        if cost.get("private_cost"):
            claimable["typical_private_cost"] = float(cost["private_cost"])
        if cost.get("public_wait_days"):
            claimable["public_median_wait_days"] = float(cost["public_wait_days"])
    if d.kind == "premium":
        for side, v in (("navy", d.navy), ("comp", d.comp)):
            if v and v.note.startswith("at $"):
                claimable[f"{side}_excess"] = float(v.note.split("$")[1].split(" ")[0].replace(",", ""))
    return {
        "fact_id": fid, "type": d.kind, "subject": {"benefit": d.key},
        "metric": d.key, "operation": None,
        "value": {"navy": _side(d.navy), "comp": _side(d.comp)},
        "unit": UNITS.get(d.kind),
        "text": f"{d.label}: {navy_name} {d.navy.display if d.navy else 'n/a'}"
                f"{' ' + d.navy.note if d.navy and d.navy.note else ''}; {comp_name} "
                f"{d.comp.display if d.comp else 'n/a'}{' ' + d.comp.note if d.comp and d.comp.note else ''}"
                + (f". Its most common procedure is {procedure}; typical_private_cost is what it typically "
                   f"costs {cost_words}, public_median_wait_days the median public wait for it"
                   if procedure else ""),
        "names": [navy_name, comp_name, d.label, "Navy Health"] + ([procedure] if procedure else []),
        "occurrences": None,
        "scope": {"state": state, "family_type": family, "direction": d.direction,
                  **({"matches_what_member_cares_about": True} if d.chip_match else {})},
        "claimable": claimable,
        "evidence_rows": [], "source": {"table": "comparison card", "snapshot_id": snapshot},
        "coverage": {"requested": d.key, "complete_for_claim": True, "reason": None},
    }


MAX_NEED_MATCHED_EXTRA = 4
MAX_EVERYDAY_EXTRA = 3


def facts(card: Card, *, navy_name: str, comp_name: str, snapshot: int, state: str, family: str,
          full: bool = False) -> list[dict]:
    """The card's facts: the price, the gaps in the everyday extras services, Navy Health's top
    strengths, the competitor's top strengths, the excess trade-off, and — beyond the usual cutoff —
    anything that matches what the member said they care about, so that fact reaches the writer even
    when it is not the single highest-scoring one.

    `full` is for a reply to pushback, not the opening pitch: the member can ask about anything on
    either policy, including a benefit the two share equally, so every benefit becomes a fact - the
    curation and the 5/3 cutoffs below are for keeping the opening pitch punchy, not for deciding what
    the writer is allowed to know when answering a direct question."""
    chosen: list[Difference] = []
    seen: set[str] = set()

    def take(d: Difference) -> None:
        if d.key not in seen:
            seen.add(d.key)
            chosen.append(d)

    if card.price and card.price.navy and card.price.comp:
        take(card.price)
    if full:
        for d in card.stronger + card.watch + card.trade_offs + card.same + card.missing:
            take(d)
    else:
        # The services nearly every member claims on (dental, optical, physio...) come straight after
        # the price: a real gap in one of those is worth more on a call than a bigger-scoring gap in a
        # service few people use, and the writer falls back to the order it is given.
        everyday = [d for d in card.stronger + card.watch
                    if d.key.split(":", 1)[0] in ("extras", "limit") and d.key.split(":", 1)[1] in EVERYDAY_SERVICES]
        everyday.sort(key=lambda d: EVERYDAY_SERVICES.index(d.key.split(":", 1)[1]))
        for d in everyday[:MAX_EVERYDAY_EXTRA]:
            take(d)
        for d in card.stronger[:5]:
            if d.key != "price":
                take(d)
        for d in card.watch[:3]:
            if d.key != "price":
                take(d)
        extra = 0
        for d in card.stronger + card.watch:
            if d.chip_match and d.key not in seen and extra < MAX_NEED_MATCHED_EXTRA:
                take(d)
                extra += 1
        for d in card.trade_offs:
            if d.key == "excess":
                take(d)
                break
    costs = category_costs()
    return [_fact(f"F{i + 1}", d, navy_name, comp_name, snapshot, state, family, costs)
            for i, d in enumerate(chosen)]
