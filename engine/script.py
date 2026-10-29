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
from engine.differences import Card, Difference

UNITS = {"premium": "AUD/month", "wait_months": "months", "extras_item": "AUD", "extras_limit": "AUD",
         "excess": "AUD", "copayment": "AUD/day", "tier": "rank"}


def _side(v: Optional[Value]):
    if v is None:
        return None
    if v.size is not None and v.size != UNLIMITED and v.kind in UNITS and v.basis != "percent":
        return v.size
    return v.display


def _fact(fid: str, d: Difference, navy_name: str, comp_name: str, snapshot: int, state: str, family: str) -> dict:
    claimable = {}
    if d.navy and d.navy.note:
        claimable["navy_note"] = d.navy.note
    if d.comp and d.comp.note:
        claimable["comp_note"] = d.comp.note
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
                f"{d.comp.display if d.comp else 'n/a'}{' ' + d.comp.note if d.comp and d.comp.note else ''}",
        "names": [navy_name, comp_name, d.label, "Navy Health"],
        "occurrences": None,
        "scope": {"state": state, "family_type": family, "direction": d.direction,
                  **({"matches_what_member_cares_about": True} if d.chip_match else {})},
        "claimable": claimable,
        "evidence_rows": [], "source": {"table": "comparison card", "snapshot_id": snapshot},
        "coverage": {"requested": d.key, "complete_for_claim": True, "reason": None},
    }


MAX_NEED_MATCHED_EXTRA = 4


def facts(card: Card, *, navy_name: str, comp_name: str, snapshot: int, state: str, family: str) -> list[dict]:
    """The card's facts: the price, Navy Health's top strengths, the competitor's top strengths, the
    excess trade-off, and — beyond the usual cutoff — anything that matches what the member said they
    care about, so that fact reaches the writer even when it is not the single highest-scoring one.
    With no chips active this is exactly the old top-5/top-3/excess selection; chips only ever add."""
    chosen: list[Difference] = []
    seen: set[str] = set()

    def take(d: Difference) -> None:
        if d.key not in seen:
            seen.add(d.key)
            chosen.append(d)

    if card.price and card.price.navy and card.price.comp:
        take(card.price)
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
    return [_fact(f"F{i + 1}", d, navy_name, comp_name, snapshot, state, family) for i, d in enumerate(chosen)]
