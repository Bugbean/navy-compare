"""Merging several policies' Values into one synthetic bundle side.

A rep can treat a hospital policy plus any number of extras policies as one product — the
way people actually buy cover: a base policy topped up with a separate extras policy,
sometimes stacked further. `merge_values` takes each member's `Value` dict (as
`engine.benefits.values()` returns it, one per real policy id) and produces one merged dict
for a bundle's synthetic id, so every downstream function (`engine.differences.card/matrix`,
`engine.claims.hospital_sheet/extras_sheet`) treats it exactly like a real policy — they only
ever read `vals[id]`, never touch the database, so a dict is as good as a row to them.

A feature key present on only one member carries straight through unchanged. This is how
excess, co-payment, tier, accommodation, waiting periods and hospital cover automatically
"belong" to whichever member is the hospital (or combined) policy: `engine.benefits.values()`
only ever builds those keys for a policy that has a hospital profile, so an extras-only
member's dict simply doesn't have them — there's nothing to merge.

A key present on more than one member (in practice: price, and any extras service/limit/item
that more than one member's policy also covers) sums where that is a real, addable quantity,
and falls back to a sane rule where it isn't — see each `_merge_*` function below.
"""
from __future__ import annotations

from typing import Optional

from engine.benefits import UNLIMITED, Value, _money

LIMIT_WORDS = {"per_person": "a person a year", "per_policy": "a policy a year",
               "per_service": "a service", "lifetime": "lifetime"}


def merge_values(parts: list[dict[str, Value]]) -> dict[str, Value]:
    """One side's worth of Values, combining every member's dict. `parts` is in the order the
    rep added them; the first part is the tie-break "leader" wherever a rule has to just pick
    one (an incompatible pair of free-text limits, say)."""
    keys: set[str] = set()
    for p in parts:
        keys.update(p)
    out: dict[str, Value] = {}
    for key in keys:
        vs = [p[key] for p in parts if key in p]
        if vs[0].kind == "premium":
            # Every member's premium has to be known for the bundle's price to mean anything —
            # unlike every other kind, one part short-circuits to "can't price this", not a
            # passthrough of whichever part happens to have it.
            merged = _merge_premium(key, vs) if len(vs) == len(parts) else None
        elif len(vs) == 1:
            merged = vs[0]
        else:
            merged = _merge_one(key, vs)
        if merged is not None:
            out[key] = merged
    return out


def _merge_one(key: str, vs: list[Value]) -> Optional[Value]:
    kind = vs[0].kind
    if kind == "extras_service":
        return _merge_service(key, vs)
    if kind == "extras_limit":
        return _merge_limit(key, vs)
    if kind == "extras_item":
        return _merge_item(key, vs)
    # Not expected to repeat across bundle members (excess, tier, waits, ...) — these only ever
    # come from a hospital profile, and a bundle realistically has one hospital member. If it
    # somehow does repeat, keep the first rather than invent a merge rule for it.
    return vs[0]


def _merge_premium(key: str, vs: list[Value]) -> Value:
    """Every member's own premium, summed — buying the bundle costs every part of it. Called
    only once every member actually supplied a price; `merge_values` omits the key entirely
    otherwise, which is what makes the existing `missing_rates` check in app/call.py pick an
    unpriceable bundle up with no extra code there."""
    total = sum(v.size for v in vs)
    notes = [v.note for v in vs if v.note and v.note != "no excess"]
    note = notes[0] if notes else vs[0].note
    return Value(key, "premium", f"{_money(total)} a month", -total, total, "dollars", note)


def _merge_service(key: str, vs: list[Value]) -> Value:
    """Covered if any member covers it; the shorter wait wins, since the member states what's
    actually needed to use the cover."""
    covered = any(v.rank == 1.0 for v in vs)
    waits = [v.wait for v in vs if v.rank == 1.0 and v.wait]
    wait = min(waits) if waits else None
    text = "Not covered" if not covered else ("Covered" if not wait else f"Covered, {wait:g} month wait")
    return Value(key, "extras_service", text, 1.0 if covered else 0.0, None, wait=wait)


def _merge_limit(key: str, vs: list[Value]) -> Value:
    unlimited = next((v for v in vs if v.size == UNLIMITED), None)
    if unlimited:
        return unlimited   # one member's "no annual limit" covers everything any other member would add
    summable = [v for v in vs if v.basis in LIMIT_WORDS and v.size]
    bases = {v.basis for v in summable}
    if summable and len(bases) == 1:
        total = sum(v.size for v in summable)
        basis = summable[0].basis
        return Value(key, "extras_limit", f"{_money(total)} {LIMIT_WORDS[basis]}", total, total, basis)
    if summable:
        # Incompatible bases on the same key (a per-person cap next to a per-policy one) — showing
        # the more generous figure beats adding dollars that are not measured the same way.
        return max(summable, key=lambda v: v.size)
    real = [v for v in vs if v.display != "Not covered"]
    if not real:
        return vs[0]
    if len(real) == 1:
        return real[0]
    displays: list[str] = []
    for v in real:
        if v.display not in displays:
            displays.append(v.display)
    return Value(key, "extras_limit", "; ".join(displays), None, None, "text")


def _merge_item(key: str, vs: list[Value]) -> Value:
    """A dollar benefit from each member adds up — two policies both paying towards the same
    item means two claims. A percent-of-fee benefit cannot stack past the fee itself, so the
    higher one wins instead of being added."""
    dollars = [v for v in vs if v.basis == "dollars"]
    if dollars:
        total = sum(v.size for v in dollars)
        return Value(key, "extras_item", _money(total), total, total, "dollars")
    percents = [v for v in vs if v.basis == "percent"]
    if percents:
        best = min(100.0, max(v.size for v in percents))
        return Value(key, "extras_item", f"{best:g}% of the fee", best, best, "percent")
    return vs[0]
