"""Feature comparison: the policies side by side, feature by feature.

No model is involved. The table is read straight from the snapshot through the same resolvers
the question engine uses, so what a rep sees is what the data says. Navy Health's policy is always
the first column. A row where the two sides agree is marked so the rep can see the differences first.
"""
from __future__ import annotations

from typing import Any, Optional

from config.reference import CLINICAL_CATEGORIES, EXTRAS_SERVICES
from engine import queries as q
from engine.db import Snapshot

NOT_OFFERED = "not offered"

_COVER_ROWS = """
SELECT n.category, n.cover AS navy, c.cover AS comp
FROM hospital_cover_for(:navy, :snap, :state) n
FULL JOIN hospital_cover_for(:comp, :snap, :state) c ON c.category = n.category
"""

_EXTRAS_ROWS = """
SELECT coalesce(n.service, c.service) AS service,
       n.covered AS navy, c.covered AS comp,
       n.wait_months AS navy_wait, c.wait_months AS comp_wait
FROM extras_services_for(:navy, :snap, :state, :family) n
FULL JOIN extras_services_for(:comp, :snap, :state, :family) c ON c.service = n.service
"""

_AMBULANCE = """
SELECT DISTINCT ambulance_cover FROM variants_for(:policy, :snap, :state)
WHERE status = 'Open'
"""

_PREMIUMS = """
SELECT excess_option, min(premium_month) AS premium, min(excess_type) AS excess_type
FROM variants_for(:policy, :snap, :state)
WHERE status = 'Open' AND family_type = :family AND dependant_scope = :scope
GROUP BY excess_option ORDER BY excess_option
"""


def _cover_word(value: Optional[str]) -> str:
    return {"Covered": "covered", "Restricted": "restricted", "NotCovered": "not covered"}.get(value or "", NOT_OFFERED)


def _row(feature: str, navy: Any, comp: Any, *, fmt=str, missing: str = NOT_OFFERED) -> dict:
    navy_v = missing if navy is None else fmt(navy)
    comp_v = missing if comp is None else fmt(comp)
    return {"feature": feature, "navy": navy_v, "comp": comp_v, "same": navy_v == comp_v}


def _money(v: Any) -> str:
    return f"${float(v):,.2f}"


def _months(v: Any) -> str:
    return f"{v} months"


def compare(db: Snapshot, *, navy: dict, comp: dict, state: str, family: str, scope: str) -> dict:
    """Every feature the two policies can be compared on, in sections. `navy` and `comp` are policy rows
    (id, current_name, product_type)."""
    state, family, scope = q._state(state), q._family(family), q._scope(scope)
    snap = db.live_snapshot_id()
    sections: list[dict] = []
    both = {"navy": navy["id"], "comp": comp["id"], "snap": snap, "state": state}

    if navy["product_type"] in ("hospital", "combined") or comp["product_type"] in ("hospital", "combined"):
        prof = {}
        for side, pid in (("navy", navy["id"]), ("comp", comp["id"])):
            r = db.rows(q._PROFILE, {"policy": pid, "snap": snap, "state": state})
            prof[side] = r[0] if r else None
        rows = []
        if prof["navy"] or prof["comp"]:
            n, c = prof["navy"] or {}, prof["comp"] or {}
            rows.append(_row("Hospital tier", n.get("hospital_tier"), c.get("hospital_tier")))
            rows.append(_row("Accommodation", n.get("accommodation"), c.get("accommodation")))
            for label, field in (("Pre-existing condition wait", "wait_pre_existing_months"),
                                 ("Other conditions wait", "wait_other_months"),
                                 ("Sub-acute wait", "wait_sub_acute_months"),
                                 ("Pregnancy wait", "wait_pregnancy_months")):
                rows.append(_row(label, n.get(field), c.get(field), fmt=_months))
        sections.append({"title": "Hospital", "rows": rows})

        cover_rows = db.rows(_COVER_ROWS, both)
        by_cat = {r["category"]: r for r in cover_rows}
        sections.append({"title": "Clinical categories", "rows": [
            _row(cat, by_cat[cat]["navy"] if cat in by_cat else None,
                 by_cat[cat]["comp"] if cat in by_cat else None, fmt=_cover_word)
            for cat in CLINICAL_CATEGORIES if cat in by_cat]})

    if navy["product_type"] in ("extras", "combined") or comp["product_type"] in ("extras", "combined"):
        ext = {r["service"]: r for r in db.rows(_EXTRAS_ROWS, {**both, "family": family})}
        sections.append({"title": "Extras", "rows": [
            _row(svc, ext[svc]["navy"] if svc in ext else None, ext[svc]["comp"] if svc in ext else None,
                 fmt=lambda v: "covered" if v else "not covered")
            for svc in EXTRAS_SERVICES if svc in ext]})

    premium_rows = []
    navy_prem = db.rows(_PREMIUMS, {"policy": navy["id"], "snap": snap, "state": state, "family": family, "scope": scope})
    comp_prem = db.rows(_PREMIUMS, {"policy": comp["id"], "snap": snap, "state": state, "family": family, "scope": scope})
    n_by = {round(float(r["excess_option"] or 0), 2): r for r in navy_prem}
    c_by = {round(float(r["excess_option"] or 0), 2): r for r in comp_prem}
    for ex in sorted(set(n_by) | set(c_by)):
        n, c = n_by.get(ex), c_by.get(ex)
        label = f"Premium a month, ${ex:,.0f} excess" if ex else "Premium a month, no excess"
        premium_rows.append(_row(label, n and n["premium"], c and c["premium"], fmt=_money,
                                 missing="no rate at this excess"))
    if not premium_rows:
        premium_rows.append(_row("Premium a month", None, None, fmt=_money))
    sections.append({"title": f"Price for this household ({family.replace('_', ' ')}, {state})",
                     "rows": premium_rows})

    amb = {"navy": db.rows(_AMBULANCE, {"policy": navy["id"], "snap": snap, "state": state, "family": family, "scope": scope}),
           "comp": db.rows(_AMBULANCE, {"policy": comp["id"], "snap": snap, "state": state, "family": family, "scope": scope})}
    sections.append({"title": "Ambulance", "rows": [
        _row("Ambulance cover", amb["navy"][0]["ambulance_cover"] if amb["navy"] else None,
             amb["comp"][0]["ambulance_cover"] if amb["comp"] else None)]})

    return {
        "navy": navy["current_name"],
        "comp": comp["current_name"],
        "state": state,
        "family": family,
        "scope": scope,
        "snapshot": snap,
        "sections": [s for s in sections if s["rows"]],
    }
