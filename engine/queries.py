"""The question types the router can answer, each as the facts it establishes.

Every answer is built from the live snapshot through the *_for() resolvers in
the database, so the 'ALL'-state rule is applied in one place (see
docs/DATA_MODEL.md). A comparison always has Navy Health's policy on the
navy side of the value and the competitor's on the comp side: a figure that
belongs to one can never be stated as the other, which is the one error a
rep must not repeat to a member.

Each function returns Fact dicts in the shape ai/facts.py validates. A figure
the database does not hold is not invented: the fact is returned with
coverage.complete_for_claim = False and a reason, so the verifier refuses any
claim that would rest on it.
"""
from __future__ import annotations

from typing import Any, Optional

from config.reference import (
    CLINICAL_CATEGORIES,
    DEPENDANT_TITLES,
    EXTRAS_ITEMS,
    FAMILY_TYPES,
    STATES,
)
from engine.db import EngineError, Snapshot

# ── input checks: a question's slots are validated before any SQL is built ──────

def _state(state: str) -> str:
    if state not in STATES or state == "ALL":
        raise EngineError(f"state must be one of the eight states, not {state!r}")
    return state


def _family(family: str) -> str:
    if family not in FAMILY_TYPES:
        raise EngineError(f"unknown family_type {family!r}")
    return family


def _scope(scope: str) -> str:
    # The canonical "+"-joined form config.reference.dependant_scope produces; checked against its titles.
    if scope == "":
        return ""
    parts = scope.split("+")
    if any(p not in DEPENDANT_TITLES for p in parts) or parts != sorted(parts):
        raise EngineError(f"dependant_scope {scope!r} is not in canonical form")
    return scope


def _category(category: str) -> str:
    if category not in CLINICAL_CATEGORIES:
        raise EngineError(f"unknown clinical category {category!r}")
    return category


# ── fact construction ──────────────────────────────────────────────────────────

def _fact(fact_id: str, type_: str, *, subject: dict, value: Any, unit: Optional[str], text: Optional[str],
          names: list[str], scope: dict, complete: bool, reason: Optional[str] = None,
          claimable: Optional[dict] = None, evidence: Optional[list] = None, table: str = "",
          snapshot: int) -> dict:
    return {
        "fact_id": fact_id,
        "type": type_,
        "subject": subject,
        "metric": type_,
        "operation": None,
        "value": value,
        "unit": unit,
        "text": text,
        "names": [n for n in names if n],
        "occurrences": None,
        "scope": scope,
        "claimable": claimable or {},
        "evidence_rows": evidence or [],
        "source": {"table": table, "snapshot_id": snapshot},
        "coverage": {"requested": fact_id, "complete_for_claim": complete, "reason": reason},
    }


def _policy(db: Snapshot, policy_id: int) -> dict:
    rows = db.rows("SELECT id, current_name, product_type, is_navy, fund_code, status FROM policies WHERE id = :id",
                   {"id": policy_id})
    if not rows:
        raise EngineError(f"no policy {policy_id}")
    return rows[0]


def _navy(db: Snapshot, policy_id: int) -> dict:
    p = _policy(db, policy_id)
    if not p["is_navy"]:
        raise EngineError(f"policy {policy_id} is not a Navy Health policy")
    return p


def _comp(db: Snapshot, policy_id: int) -> dict:
    p = _policy(db, policy_id)
    if p["is_navy"]:
        raise EngineError(f"policy {policy_id} is Navy Health's; a competitor was expected")
    return p


# ── price: what each policy costs for a household, by excess option ───────────

_RATES = """
SELECT v.excess_type, v.excess_option, v.premium_month, v.copayment_type, v.state,
       v.family_type, v.dependant_scope, v.status
FROM variants_for(:policy, :snap, :state) v
WHERE v.family_type = :family AND v.dependant_scope = :scope AND v.status = 'Open'
ORDER BY v.excess_option, v.premium_month
"""


def _rates(db: Snapshot, snap: int, policy: dict, state: str, family: str, scope: str) -> list[dict]:
    return db.rows(_RATES, {"policy": policy["id"], "snap": snap, "state": state, "family": family, "scope": scope})


def price(db: Snapshot, *, navy_policy_id: int, comp_policy_id: Optional[int], state: str, family: str,
          scope: str) -> list[dict]:
    """The monthly premium of each policy for this household, one fact per excess option, and
    the gap where both have the same excess option."""
    state, family, scope = _state(state), _family(family), _scope(scope)
    snap = db.live_snapshot_id()
    navy = _navy(db, navy_policy_id)
    out: list[dict] = []
    sides = {"navy": navy}
    if comp_policy_id is not None:
        sides["comp"] = _comp(db, comp_policy_id)
    by_side: dict[str, dict[str, dict]] = {}
    for side, policy in sides.items():
        rates = _rates(db, snap, policy, state, family, scope)
        by_side[side] = {}
        for r in rates:
            key = f"{r['excess_type']}:{r['excess_option']}"
            by_side[side].setdefault(key, r)
        if not rates:
            out.append(_fact(
                f"price.{side}.{policy['id']}.{state}.{family}.{scope or 'none'}", "price",
                subject={"side": side, "policy_id": policy["id"]}, value=None, unit="AUD/month",
                text=None, names=[policy["current_name"]],
                scope={"state": state, "family_type": family, "dependant_scope": scope},
                complete=False, reason="no open rate for this household in this state",
                table="variants", snapshot=snap))
        for key, r in by_side[side].items():
            out.append(_fact(
                f"price.{side}.{policy['id']}.{state}.{family}.{scope or 'none'}.{key}", "price",
                subject={"side": side, "policy_id": policy["id"]}, value=float(r["premium_month"]),
                unit="AUD/month", text=None, names=[policy["current_name"]],
                scope={"state": state, "family_type": family, "dependant_scope": scope,
                       "excess_type": r["excess_type"], "excess": float(r["excess_option"] or 0)},
                complete=True, claimable={"excess": float(r["excess_option"] or 0)},
                table="variants", snapshot=snap))
    if comp_policy_id is not None:
        for key in sorted(set(by_side["navy"]) & set(by_side["comp"])):
            n, c = by_side["navy"][key], by_side["comp"][key]
            out.append(_fact(
                f"price_gap.{navy['id']}.{comp_policy_id}.{state}.{family}.{scope or 'none'}.{key}", "price_gap",
                subject={"navy_policy_id": navy["id"], "comp_policy_id": comp_policy_id},
                value={"navy": float(n["premium_month"]), "comp": float(c["premium_month"])},
                unit="AUD/month", text=None, names=[navy["current_name"], sides["comp"]["current_name"]],
                scope={"state": state, "family_type": family, "dependant_scope": scope,
                       "excess": float(n["excess_option"] or 0)},
                complete=True, claimable={"excess": float(n["excess_option"] or 0)},
                table="variants", snapshot=snap))
    return out


# ── is_covered: one clinical category, one policy ─────────────────────────────

_COVER = """
SELECT c.category, c.cover, p.hospital_tier, p.accommodation, p.state AS profile_state
FROM hospital_cover_for(:policy, :snap, :state) c
JOIN hospital_profile_for(:policy, :snap, :state) p ON true
WHERE c.category = :category
"""


def is_covered(db: Snapshot, *, policy_id: int, category: str, state: str) -> list[dict]:
    state, category = _state(state), _category(category)
    snap = db.live_snapshot_id()
    policy = _policy(db, policy_id)
    rows = db.rows(_COVER, {"policy": policy_id, "snap": snap, "state": state, "category": category})
    if not rows:
        return [_fact(f"is_covered.{policy_id}.{state}.{category}", "is_covered",
                      subject={"side": "navy" if policy["is_navy"] else "comp", "policy_id": policy_id},
                      value=None, unit=None, text=None, names=[policy["current_name"]],
                      scope={"state": state, "category": category}, complete=False,
                      reason="no hospital cover recorded for this policy in this state",
                      table="hospital_cover", snapshot=snap)]
    r = rows[0]
    return [_fact(f"is_covered.{policy_id}.{state}.{category}", "is_covered",
                  subject={"side": "navy" if policy["is_navy"] else "comp", "policy_id": policy_id},
                  value=r["cover"], unit=None, text=f"{category}: {r['cover']}",
                  names=[policy["current_name"]],
                  scope={"state": state, "category": category, "hospital_tier": r["hospital_tier"]},
                  complete=True, table="hospital_cover", snapshot=snap)]


# ── head to head: cover, tier and waits, Navy against one competitor ──────────

_CATEGORY_LEVELS = """
SELECT n.category, n.cover AS navy_cover, c.cover AS comp_cover
FROM hospital_cover_for(:navy, :snap, :state) n
JOIN hospital_cover_for(:comp, :snap, :state) c ON c.category = n.category
ORDER BY n.category
"""

_PROFILE = """
SELECT hospital_tier, tier_rank, accommodation, wait_pre_existing_months, wait_other_months,
       wait_sub_acute_months, wait_pregnancy_months, state
FROM hospital_profile_for(:policy, :snap, :state)
"""

_EXTRAS_COUNT = """
SELECT count(*) FILTER (WHERE covered) AS covered, count(*) AS total
FROM extras_services_for(:policy, :snap, :state, :family)
"""


def head_to_head(db: Snapshot, *, navy_policy_id: int, comp_policy_id: int, state: str, family: str,
                 scope: str) -> list[dict]:
    """Cover, tier, accommodation, waits and extras services, Navy against the competitor, then the
    premium for this household (see price)."""
    state, family, scope = _state(state), _family(family), _scope(scope)
    snap = db.live_snapshot_id()
    navy, comp = _navy(db, navy_policy_id), _comp(db, comp_policy_id)
    names = [navy["current_name"], comp["current_name"]]
    subject = {"navy_policy_id": navy_policy_id, "comp_policy_id": comp_policy_id}
    scope_dict = {"state": state, "family_type": family, "dependant_scope": scope}
    out: list[dict] = []

    if navy["product_type"] in ("hospital", "combined") and comp["product_type"] in ("hospital", "combined"):
        cats = db.rows(_CATEGORY_LEVELS, {"navy": navy_policy_id, "comp": comp_policy_id, "snap": snap, "state": state})
        same = [c for c in cats if c["navy_cover"] == c["comp_cover"]]
        differ = [c for c in cats if c["navy_cover"] != c["comp_cover"]]
        out.append(_fact(f"hospital_cover_count.{navy_policy_id}.{comp_policy_id}.{state}", "hospital_cover_count",
                         subject=subject, value={"navy": sum(c["navy_cover"] == "Covered" for c in cats),
                                                 "comp": sum(c["comp_cover"] == "Covered" for c in cats)},
                         unit="categories", text="categories covered in full", names=names,
                         scope=scope_dict, complete=bool(cats), reason=None if cats else "no hospital cover",
                         claimable={"same": len(same), "differ": len(differ)},
                         evidence=[dict(c) for c in differ], table="hospital_cover", snapshot=snap))
        navy_profile = db.rows(_PROFILE, {"policy": navy_policy_id, "snap": snap, "state": state})
        comp_profile = db.rows(_PROFILE, {"policy": comp_policy_id, "snap": snap, "state": state})
        np_ = navy_profile[0] if navy_profile else None
        cp_ = comp_profile[0] if comp_profile else None
        if np_ and cp_:
            out.append(_fact(f"tier.{navy_policy_id}.{comp_policy_id}.{state}", "tier", subject=subject,
                             value={"navy": np_["tier_rank"], "comp": cp_["tier_rank"]}, unit="rank",
                             text=None, names=names, scope=scope_dict, complete=True,
                             claimable={"navy_tier": np_["hospital_tier"], "comp_tier": cp_["hospital_tier"]},
                             table="hospital_profiles", snapshot=snap))
            for field, label in (("wait_pre_existing_months", "PreExisting"), ("wait_other_months", "Other"),
                                 ("wait_sub_acute_months", "SubAcute"), ("wait_pregnancy_months", "PregnancyBirth")):
                if np_[field] is None and cp_[field] is None:
                    continue
                out.append(_fact(f"wait.{label}.{navy_policy_id}.{comp_policy_id}.{state}", "waiting_period",
                                 subject=subject, value={"navy": np_[field], "comp": cp_[field]}, unit="months",
                                 text=label, names=names, scope=scope_dict, complete=True,
                                 table="hospital_profiles", snapshot=snap))

    if navy["product_type"] in ("extras", "combined") and comp["product_type"] in ("extras", "combined"):
        n = db.rows(_EXTRAS_COUNT, {"policy": navy_policy_id, "snap": snap, "state": state, "family": family})
        c = db.rows(_EXTRAS_COUNT, {"policy": comp_policy_id, "snap": snap, "state": state, "family": family})
        if n and c and n[0]["total"] and c[0]["total"]:
            out.append(_fact(f"extras_services_count.{navy_policy_id}.{comp_policy_id}.{state}", "extras_services_count",
                             subject=subject, value={"navy": int(n[0]["covered"]), "comp": int(c[0]["covered"])},
                             unit="services", text="extras services covered", names=names, scope=scope_dict,
                             complete=True, table="extras_services", snapshot=snap))

    prices = price(db, navy_policy_id=navy_policy_id, comp_policy_id=comp_policy_id, state=state,
                   family=family, scope=scope)
    out.extend(prices)

    # Premiums are only comparable at the same excess. When the two policies share no excess option, the
    # answer must say so, with both excesses, or a rep will read a gap that is really a difference in excess.
    navy_excess = sorted({f["scope"]["excess"] for f in prices
                          if f["subject"].get("side") == "navy" and "excess" in f["scope"]})
    comp_excess = sorted({f["scope"]["excess"] for f in prices
                          if f["subject"].get("side") == "comp" and "excess" in f["scope"]})
    if navy_excess and comp_excess and not set(navy_excess) & set(comp_excess):
        out.append(_fact(
            f"excess_mismatch.{navy_policy_id}.{comp_policy_id}.{state}.{family}", "excess_mismatch",
            subject=subject, value={"navy": navy_excess[0], "comp": comp_excess[0]}, unit="AUD",
            text="the premiums are at different excess levels", names=names, scope=scope_dict, complete=True,
            claimable={"navy_excess_options": navy_excess, "comp_excess_options": comp_excess},
            table="variants", snapshot=snap))
    return out


# ── waiting periods and accommodation, one policy ─────────────────────────────

def accommodation(db: Snapshot, *, policy_id: int, state: str) -> list[dict]:
    state = _state(state)
    snap = db.live_snapshot_id()
    policy = _policy(db, policy_id)
    rows = db.rows(_PROFILE, {"policy": policy_id, "snap": snap, "state": state})
    side = "navy" if policy["is_navy"] else "comp"
    if not rows:
        return [_fact(f"accommodation.{policy_id}.{state}", "accommodation", subject={"side": side, "policy_id": policy_id},
                      value=None, unit=None, text=None, names=[policy["current_name"]], scope={"state": state},
                      complete=False, reason="no hospital profile for this policy in this state",
                      table="hospital_profiles", snapshot=snap)]
    r = rows[0]
    return [_fact(f"accommodation.{policy_id}.{state}", "accommodation", subject={"side": side, "policy_id": policy_id},
                  value=r["accommodation"], unit=None, text=r["accommodation"], names=[policy["current_name"]],
                  scope={"state": state, "hospital_tier": r["hospital_tier"]}, complete=True,
                  table="hospital_profiles", snapshot=snap)]


def waiting(db: Snapshot, *, policy_id: int, state: str) -> list[dict]:
    state = _state(state)
    snap = db.live_snapshot_id()
    policy = _policy(db, policy_id)
    side = "navy" if policy["is_navy"] else "comp"
    rows = db.rows(_PROFILE, {"policy": policy_id, "snap": snap, "state": state})
    if not rows:
        return [_fact(f"waiting.{policy_id}.{state}", "waiting_period", subject={"side": side, "policy_id": policy_id},
                      value=None, unit="months", text=None, names=[policy["current_name"]], scope={"state": state},
                      complete=False, reason="no hospital profile for this policy in this state",
                      table="hospital_profiles", snapshot=snap)]
    r = rows[0]
    out = []
    for field, label in (("wait_pre_existing_months", "PreExisting"), ("wait_other_months", "Other"),
                         ("wait_sub_acute_months", "SubAcute"), ("wait_pregnancy_months", "PregnancyBirth")):
        out.append(_fact(f"wait.{label}.{policy_id}.{state}", "waiting_period",
                         subject={"side": side, "policy_id": policy_id}, value=r[field], unit="months",
                         text=label, names=[policy["current_name"]], scope={"state": state}, complete=True,
                         table="hospital_profiles", snapshot=snap))
    return out


# ── excess options and ambulance: from the rate card ───────────────────────────

_EXCESS = """
SELECT DISTINCT excess_type, excess_option, excess_per_person, excess_per_policy, excess_per_admission
FROM variants_for(:policy, :snap, :state)
WHERE family_type = :family AND dependant_scope = :scope AND status = 'Open'
ORDER BY excess_option
"""

_AMBULANCE = """
SELECT DISTINCT ambulance_cover, ambulance_use_fund
FROM variants_for(:policy, :snap, :state)
WHERE status = 'Open'
"""


def excess_options(db: Snapshot, *, policy_id: int, state: str, family: str, scope: str) -> list[dict]:
    state, family, scope = _state(state), _family(family), _scope(scope)
    snap = db.live_snapshot_id()
    policy = _policy(db, policy_id)
    side = "navy" if policy["is_navy"] else "comp"
    rows = db.rows(_EXCESS, {"policy": policy_id, "snap": snap, "state": state, "family": family, "scope": scope})
    if not rows:
        return [_fact(f"excess.{policy_id}.{state}.{family}", "excess_option", subject={"side": side, "policy_id": policy_id},
                      value=None, unit="AUD", text=None, names=[policy["current_name"]],
                      scope={"state": state, "family_type": family, "dependant_scope": scope}, complete=False,
                      reason="no open rate for this household in this state", table="variants", snapshot=snap)]
    return [_fact(f"excess.{policy_id}.{state}.{family}.{r['excess_option']}", "excess_option",
                  subject={"side": side, "policy_id": policy_id}, value=float(r["excess_option"] or 0), unit="AUD",
                  text=r["excess_type"], names=[policy["current_name"]],
                  scope={"state": state, "family_type": family, "dependant_scope": scope,
                         "excess_type": r["excess_type"]},
                  complete=True, claimable={"per_person": r["excess_per_person"],
                                            "per_policy": r["excess_per_policy"],
                                            "per_admission": r["excess_per_admission"]},
                  table="variants", snapshot=snap) for r in rows]


def ambulance(db: Snapshot, *, policy_id: int, state: str) -> list[dict]:
    state = _state(state)
    snap = db.live_snapshot_id()
    policy = _policy(db, policy_id)
    side = "navy" if policy["is_navy"] else "comp"
    rows = db.rows(_AMBULANCE, {"policy": policy_id, "snap": snap, "state": state})
    if not rows:
        return [_fact(f"ambulance.{policy_id}.{state}", "ambulance", subject={"side": side, "policy_id": policy_id},
                      value=None, unit=None, text=None, names=[policy["current_name"]], scope={"state": state},
                      complete=False, reason="no rate card for this policy in this state", table="variants", snapshot=snap)]
    return [_fact(f"ambulance.{policy_id}.{state}.{r['ambulance_cover']}", "ambulance",
                  subject={"side": side, "policy_id": policy_id}, value=r["ambulance_cover"], unit=None,
                  text=r["ambulance_cover"], names=[policy["current_name"]],
                  scope={"state": state, "from_fund": bool(r["ambulance_use_fund"])}, complete=True,
                  table="variants", snapshot=snap) for r in rows]


# ── closest matches and closed products ───────────────────────────────────────

_CLOSEST = """
SELECT m.rank, m.score, n.current_name AS navy_name, n.id AS navy_id
FROM closest_matches m JOIN policies n ON n.id = m.navy_policy_id
WHERE m.snapshot_id = :snap AND m.comp_policy_id = :comp AND m.state = :state
  AND m.family_type = :family AND m.dependant_scope = :scope
ORDER BY m.rank
"""


def best_fit(db: Snapshot, *, comp_policy_id: int, state: str, family: str, scope: str) -> list[dict]:
    """The Navy Health policies that come closest to this competitor's, for this household."""
    state, family, scope = _state(state), _family(family), _scope(scope)
    snap = db.live_snapshot_id()
    comp = _comp(db, comp_policy_id)
    rows = db.rows(_CLOSEST, {"snap": snap, "comp": comp_policy_id, "state": state, "family": family, "scope": scope})
    if not rows:
        return [_fact(f"best_fit.{comp_policy_id}.{state}.{family}", "best_fit",
                      subject={"side": "comp", "comp_policy_id": comp_policy_id}, value=None, unit="similarity",
                      text=None, names=[comp["current_name"]],
                      scope={"state": state, "family_type": family, "dependant_scope": scope}, complete=False,
                      reason="no Navy Health policy has a rate for this household in this state",
                      table="closest_matches", snapshot=snap)]
    return [_fact(f"best_fit.{comp_policy_id}.{state}.{family}.{r['rank']}", "best_fit",
                  subject={"side": "navy", "navy_policy_id": r["navy_id"], "comp_policy_id": comp_policy_id},
                  value=float(r["score"]), unit="similarity", text=f"match {r['rank']}",
                  names=[r["navy_name"], comp["current_name"]],
                  scope={"state": state, "family_type": family, "dependant_scope": scope, "rank": r["rank"]},
                  complete=True, claimable={"rank": r["rank"]}, table="closest_matches", snapshot=snap)
            for r in rows]


def closed_product(db: Snapshot, *, policy_id: int) -> list[dict]:
    snap = db.live_snapshot_id()
    policy = _policy(db, policy_id)
    rows = db.rows("SELECT status, last_seen::text AS last_seen FROM policies WHERE id = :id", {"id": policy_id})
    status = rows[0]["status"]
    return [_fact(f"status.{policy_id}", "closed_product",
                  subject={"side": "navy" if policy["is_navy"] else "comp", "policy_id": policy_id},
                  value=status, unit=None, text=status, names=[policy["current_name"]], scope={},
                  complete=True, claimable={"last_seen": rows[0]["last_seen"]}, table="policies", snapshot=snap)]


# ── extras: the benefit for one item, and the limit that applies to it ───────────

_ITEM = """
SELECT b.service, b.item, b.benefit_amount, b.benefit_pct, b.state, b.family_type
FROM extras_benefits_for(:policy, :snap, :state, :family) b
WHERE b.item = :item
"""


def extras_item(db: Snapshot, *, policy_id: int, item: str, state: str, family: str) -> list[dict]:
    if item not in EXTRAS_ITEMS:
        raise EngineError(f"unknown extras item {item!r}")
    state, family = _state(state), _family(family)
    snap = db.live_snapshot_id()
    policy = _policy(db, policy_id)
    side = "navy" if policy["is_navy"] else "comp"
    _, label = EXTRAS_ITEMS[item]
    rows = db.rows(_ITEM, {"policy": policy_id, "snap": snap, "state": state, "family": family, "item": item})
    if not rows:
        return [_fact(f"item.{policy_id}.{item}.{state}.{family}", "extras_item",
                      subject={"side": side, "policy_id": policy_id}, value=None, unit="AUD",
                      text=label, names=[policy["current_name"]], scope={"state": state, "family_type": family},
                      complete=False, reason="this policy does not list a benefit for this item",
                      table="extras_benefits", snapshot=snap)]
    r = rows[0]
    return [_fact(f"item.{policy_id}.{item}.{state}.{family}", "extras_item",
                  subject={"side": side, "policy_id": policy_id}, value=r["benefit_amount"], unit="AUD",
                  text=label, names=[policy["current_name"]],
                  scope={"state": state, "family_type": family, "service": r["service"]}, complete=True,
                  claimable={"percent": r["benefit_pct"]} if r["benefit_pct"] is not None else {},
                  table="extras_benefits", snapshot=snap)]
