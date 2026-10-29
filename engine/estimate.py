"""What a member would get back from each policy's extras in a year, for the way they actually use them.

A usage profile is a short list: which billed item (engine reference EXTRAS_ITEMS), how many times a
year, for how many people in the household. For each policy this works out, per claim, what the fund
pays (its dollar benefit, or its percentage of a typical fee), then applies the policy's real limits
in the order they bite: a per-service cap on each claim, a count limit ("2 appliances every 3 years"),
then the annual limit for the group of services that share it, per person and per policy.

Every figure comes from the government data already read by engine/benefits.fetch, except the typical
fee a percentage benefit is taken of, which comes from config/typical_fees.yaml (and the rep can
change) and is always reported back as an assumption. The model's only part is turning what the rep
types into a profile (ai/usage.py); it never computes anything here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from config.reference import EXTRAS_ITEMS
from engine.claims import short_item

FEES_FILE = Path(__file__).resolve().parent.parent / "config" / "typical_fees.yaml"
MAX_USES = 30
MAX_TIMES = 52
MAX_PEOPLE = 8


@dataclass
class Use:
    item: str
    times: int
    people: int = 1
    fee: Optional[float] = None      # the fee per visit or item; the typical fee when not given


@dataclass
class Line:
    item: str
    label: str
    service: str
    claims: int                      # times x people
    fee: Optional[float]
    per_claim: float                 # what the fund pays on one claim, before the annual limit
    paid: float                      # what it pays across the year, after every limit
    note: str = ""


@dataclass
class PolicyEstimate:
    policy_id: int
    total: float
    lines: list[Line] = field(default_factory=list)


def typical_fees() -> dict[str, dict]:
    """item -> {fee, source, url}. Empty if the file is missing; percentage benefits then cannot be
    estimated and say so."""
    if not FEES_FILE.exists():
        return {}
    raw = yaml.safe_load(FEES_FILE.read_text(encoding="utf-8")) or {}
    return {k: v for k, v in raw.items() if k in EXTRAS_ITEMS and isinstance(v, dict) and v.get("fee")}


def clean_uses(raw: object) -> list[Use]:
    """A usage profile from untrusted input (the page, or the model): unknown items dropped, counts
    clamped to sensible bounds, at most MAX_USES lines."""
    out: list[Use] = []
    if not isinstance(raw, list):
        return out
    for r in raw[:MAX_USES]:
        if not isinstance(r, dict) or r.get("item") not in EXTRAS_ITEMS:
            continue
        try:
            times = max(0, min(MAX_TIMES, int(r.get("times") or 0)))
            people = max(1, min(MAX_PEOPLE, int(r.get("people") or 1)))
            fee = float(r["fee"]) if r.get("fee") not in (None, "") else None
        except (TypeError, ValueError):
            continue
        if fee is not None and not 0 < fee <= 20000:
            fee = None
        if times:
            out.append(Use(item=r["item"], times=times, people=people, fee=fee))
    return out


def _money(v: float) -> str:
    return f"${v:,.2f}" if v != int(v) else f"${v:,.0f}"


def estimate(data: dict, policy_id: int, uses: list[Use], fees: Optional[dict[str, dict]] = None) -> PolicyEstimate:
    """What `policy_id` pays back for `uses` over one year, from the data engine/benefits.fetch read
    (for this household and state). Waiting periods are reported, not applied: whether one is served
    already is not something the data can say."""
    fees = typical_fees() if fees is None else fees
    covered = {e["service"]: e for e in data["extras"] if e["policy_id"] == policy_id}
    benefits = {b["item"]: b for b in data["benefits"] if b["policy_id"] == policy_id}
    groups = [g for g in data["limits"] if g["policy_id"] == policy_id]
    group_of = {s: g for g in groups for s in (g["services"] or [])}

    lines: list[Line] = []
    # (line index, person index) -> amount before the group limits
    portions: dict[tuple[int, int], float] = {}
    counted: dict[tuple[int, int], int] = {}     # (group, person) -> claims a count limit has allowed
    for u in uses:
        svc, _ = EXTRAS_ITEMS[u.item]
        fee = u.fee if u.fee is not None else (fees.get(u.item) or {}).get("fee")
        fee = float(fee) if fee is not None else None
        line = Line(item=u.item, label=short_item(u.item), service=svc, claims=u.times * u.people,
                    fee=fee, per_claim=0.0, paid=0.0)
        lines.append(line)
        e = covered.get(svc)
        if not e or not e["covered"]:
            line.note = "not covered"
            continue
        b = benefits.get(u.item)
        if b and b["amount"] is not None:
            per = float(b["amount"]) if fee is None else min(float(b["amount"]), fee)
        elif b and b["pct"] is not None:
            if fee is None:
                line.note = f"pays {float(b['pct']):g}% of the fee; no typical fee to estimate from"
                continue
            per = round(float(b["pct"]) / 100 * fee, 2)
            line.note = f"{float(b['pct']):g}% of a {_money(fee)} fee"
        else:
            line.note = "covered, but no benefit published for this item"
            continue
        g = group_of.get(svc)
        if g and g["per_service"] is not None:
            per = min(per, float(g["per_service"]))
        line.per_claim = per
        for person in range(u.people):
            times = u.times
            if g and g["count"]:
                # A count limit is per person across every line in the group: two lines of one hearing
                # aid each still only get the one the policy allows.
                key = (id(g), person)
                times = max(0, min(times, int(g["count"]) - counted.get(key, 0)))
                counted[key] = counted.get(key, 0) + times
                if times < u.times and "limited to" not in line.note:
                    line.note = _join(line.note, f"limited to {g['count']} a person")
            portions[(len(lines) - 1, person)] = per * times
        if e.get("wait_months"):
            line.note = _join(line.note, f"{e['wait_months']} month wait for new members")

    # The annual limit of each group, per person then per policy, shared across the lines in it.
    for g in groups:
        members = {i for i, ln in enumerate(lines) if group_of.get(ln.service) is g}
        if not members:
            continue
        cap_person = _first(g, ("per_person", "lifetime"))
        if g["no_limit"]:
            cap_person = None
        if cap_person is not None:
            people = {p for (i, p) in portions if i in members}
            for p in people:
                keys = [(i, q) for (i, q) in portions if i in members and q == p]
                _scale(portions, keys, cap_person, lines)
        if g["per_policy"] is not None and not g["no_limit"]:
            _scale(portions, [k for k in portions if k[0] in members], float(g["per_policy"]), lines)
        if g["free_text"]:
            for i in members:
                lines[i].note = _join(lines[i].note, f"limit: {g['free_text']}")
    for i, ln in enumerate(lines):
        ln.paid = round(sum(v for (j, _), v in portions.items() if j == i), 2)
        if ln.service in covered and covered[ln.service]["covered"] and ln.service not in group_of:
            ln.note = _join(ln.note, "no annual limit published")
    return PolicyEstimate(policy_id=policy_id, total=round(sum(ln.paid for ln in lines), 2), lines=lines)


def _first(g: dict, fields: tuple[str, ...]) -> Optional[float]:
    for f in fields:
        if g[f] is not None:
            return float(g[f])
    return None


def _scale(portions: dict, keys: list, cap: float, lines: list[Line]) -> None:
    total = sum(portions[k] for k in keys)
    if total <= cap or total == 0:
        return
    for k in keys:
        portions[k] = portions[k] * cap / total
    for i in {k[0] for k in keys}:
        if "annual limit" not in lines[i].note:
            lines[i].note = _join(lines[i].note, f"capped by the {_money(cap)} annual limit")


def _join(a: str, b: str) -> str:
    return f"{a}; {b}" if a else b
