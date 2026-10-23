"""Feature comparison of any number of policies, feature by feature.

No model is involved: the table is read from the snapshot through the same resolvers the question
engine uses. Navy Health's policies come first, then the others, each in the order given. A row is
marked as agreeing when every column holds the same value, so the differences are what stands out.

Premiums are compared only at the same excess level: a column with no rate at a given excess says
so, rather than being set against another policy's premium at a different excess.
"""
from __future__ import annotations

from typing import Any, Optional

from config.reference import CLINICAL_CATEGORIES, EXTRAS_SERVICES
from engine import queries as q
from engine.db import EngineError, Snapshot

NOT_OFFERED = "not offered"
NO_RATE = "no rate at this excess"

_COVER = """
SELECT category, cover FROM hospital_cover_for(:policy, :snap, :state)
"""

_EXTRAS = """
SELECT service, covered FROM extras_services_for(:policy, :snap, :state, :family)
"""

_AMBULANCE = """
SELECT DISTINCT ambulance_cover FROM variants_for(:policy, :snap, :state) WHERE status = 'Open'
"""

_PREMIUMS = """
SELECT excess_option, min(premium_month) AS premium
FROM variants_for(:policy, :snap, :state)
WHERE status = 'Open' AND family_type = :family AND dependant_scope = :scope
GROUP BY excess_option ORDER BY excess_option
"""


def _cover_word(v: Optional[str]) -> str:
    return {"Covered": "covered", "Restricted": "restricted", "NotCovered": "not covered"}.get(v or "", NOT_OFFERED)


def _money(v: Any) -> str:
    return f"${float(v):,.2f}"


def _months(v: Any) -> str:
    return f"{v} months"


def _row(feature: str, values: list[Optional[str]], missing: str = NOT_OFFERED) -> dict:
    cells = [missing if v is None else v for v in values]
    return {"feature": feature, "cells": cells, "same": len(set(cells)) == 1}


def order(policies: list[dict]) -> list[dict]:
    """Navy Health first, then the rest, each group in the order given."""
    return [p for p in policies if p["is_navy"]] + [p for p in policies if not p["is_navy"]]


def compare_many(db: Snapshot, *, policies: list[dict], state: str, family: str, scope: str) -> dict:
    """The table for these policies. Each policy is a dict with id, current_name, product_type, is_navy,
    and fund_name for display."""
    if not policies:
        raise EngineError("choose at least one policy to compare")
    state, family, scope = q._state(state), q._family(family), q._scope(scope)
    cols = order(policies)
    snap = db.live_snapshot_id()
    base = {"snap": snap, "state": state}
    sections: list[dict] = []

    if any(p["product_type"] in ("hospital", "combined") for p in cols):
        profiles = []
        covers = []
        for p in cols:
            found = db.rows(q._PROFILE, {"policy": p["id"], **base})
            profiles.append(found[0] if found else {})
            covers.append({r["category"]: r["cover"] for r in db.rows(_COVER, {"policy": p["id"], **base})})
        rows = [
            _row("Hospital tier", [pr.get("hospital_tier") for pr in profiles]),
            _row("Accommodation", [pr.get("accommodation") for pr in profiles]),
        ]
        for label, field in (("Pre-existing condition wait", "wait_pre_existing_months"),
                             ("Other conditions wait", "wait_other_months"),
                             ("Sub-acute wait", "wait_sub_acute_months"),
                             ("Pregnancy wait", "wait_pregnancy_months")):
            rows.append(_row(label, [None if pr.get(field) is None else f"{pr[field]} months" for pr in profiles]))
        sections.append({"title": "Hospital", "rows": rows})
        sections.append({"title": "Clinical categories", "rows": [
            _row(cat, [_cover_word(c.get(cat)) if c else None for c in covers])
            for cat in CLINICAL_CATEGORIES if any(cat in c for c in covers)]})

    if any(p["product_type"] in ("extras", "combined") for p in cols):
        per = [{r["service"]: r["covered"] for r in db.rows(_EXTRAS, {"policy": p["id"], **base, "family": family})}
               for p in cols]
        sections.append({"title": "Extras", "rows": [
            _row(svc, [(("covered" if d[svc] else "not covered") if svc in d else None) for d in per])
            for svc in EXTRAS_SERVICES if any(svc in d for d in per)]})

    premiums = [{round(float(r["excess_option"] or 0), 2): r["premium"] for r in db.rows(
        _PREMIUMS, {"policy": p["id"], **base, "family": family, "scope": scope})} for p in cols]
    excesses = sorted({ex for d in premiums for ex in d})
    price_rows = [_row(f"Premium a month, ${ex:,.0f} excess" if ex else "Premium a month, no excess",
                       [_money(d[ex]) if ex in d else None for d in premiums], missing=NO_RATE)
                  for ex in excesses]
    if not price_rows:
        price_rows = [_row("Premium a month", [None] * len(cols), missing=NO_RATE)]
    sections.append({"title": f"Price for this household ({family.replace('_', ' ')}, {state})", "rows": price_rows})

    ambulance = []
    for p in cols:
        found = [r["ambulance_cover"] for r in db.rows(_AMBULANCE, {"policy": p["id"], **base})]
        ambulance.append(", ".join(sorted(found)) if found else None)
    sections.append({"title": "Ambulance", "rows": [_row("Ambulance cover", ambulance)]})

    return {
        "columns": [{"id": p["id"], "name": p["current_name"], "fund": p.get("fund_name", ""),
                     "is_navy": p["is_navy"]} for p in cols],
        "state": state, "family": family, "scope": scope, "snapshot": snap,
        "sections": [s for s in sections if s["rows"]],
    }
