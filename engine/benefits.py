"""Every comparable benefit of a set of policies, read in one database round trip.

`fetch` reads the hospital profile, the 38 categories, the extras services, benefits and limits,
and the household's rates for each policy through the *_for() resolvers, so the state and household
rules are the ones the rest of the engine uses. `values` turns that into one Value per feature key
per policy: what to show, and how it ranks against the same feature of another policy.

A Value's rank is only comparable with another Value of the same `basis`: a limit per person is
not ranked against a limit per policy, and a percentage benefit not against a dollar one. Where a
rank is None the two can only be shown, not ranked, and the comparison says "different".
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

from config.features import FEATURES, SERVICE_LABELS
from config.reference import EXTRAS_ITEMS, EXTRAS_SERVICES, HOSPITAL_WAITING_PERIOD_TITLES
from engine import queries as q
from engine.db import EngineError, Snapshot, lit

UNLIMITED = math.inf

TIER_LABELS = {"Basic": "Basic", "BasicPlus": "Basic Plus", "Bronze": "Bronze", "BronzePlus": "Bronze Plus",
               "Silver": "Silver", "SilverPlus": "Silver Plus", "Gold": "Gold"}
ACCOMMODATION_LABELS = {
    "PrivateOrPublic": "Private or public hospital",
    "PrivateSharedPublic": "Private hospital (shared room) or public",
    "PrivateSharedPublicShared": "Private (shared room) or public (shared room)",
    "PrivatePublicShared": "Private hospital, or public (shared room)",
    "Public": "Public hospital only",
    "PublicShared": "Public hospital, shared room",
}
AMBULANCE_LABELS = {"None": "Not covered", "EmergencyOnly": "Emergency only",
                    "EmergencyAndNonEmergency": "Emergency and non-emergency",
                    "StateGovt": "State government scheme"}
AMBULANCE_RANK = {"None": 0, "EmergencyOnly": 1, "EmergencyAndNonEmergency": 2}
COVER_LABELS = {"Covered": "Covered", "Restricted": "Restricted", "NotCovered": "Not covered"}
COVER_RANK = {"NotCovered": 0, "Restricted": 1, "Covered": 2}


@dataclass(frozen=True)
class Value:
    key: str
    kind: str                  # the difference type it is scored as (config/materiality.yaml)
    display: str
    rank: Optional[float]      # higher is better; None when it cannot be ranked
    size: Optional[float] = None   # the quantity a difference is measured in (dollars, months, steps)
    basis: str = ""            # ranks only compare within one basis
    note: str = ""             # what the rep must know beside the figure (the excess a premium is at)


_FETCH = """
WITH ids AS (SELECT unnest({ids}::bigint[]) AS id)
SELECT json_build_object(
  'release_date', (SELECT release_date::text FROM snapshots WHERE id = :snap),
  'policies', (SELECT coalesce(json_agg(json_build_object(
                   'id', p.id, 'name', p.current_name, 'product_type', p.product_type, 'is_navy', p.is_navy,
                   'fund_code', p.fund_code, 'fund_name', f.name, 'status', p.status)), '[]'::json)
               FROM policies p JOIN funds f ON f.fund_code = p.fund_code WHERE p.id IN (SELECT id FROM ids)),
  'profiles', (SELECT coalesce(json_agg(json_build_object(
                   'policy_id', i.id, 'hospital_tier', h.hospital_tier, 'tier_rank', h.tier_rank,
                   'accommodation', h.accommodation, 'PreExisting', h.wait_pre_existing_months,
                   'Other', h.wait_other_months, 'SubAcute', h.wait_sub_acute_months,
                   'PregnancyBirth', h.wait_pregnancy_months, 'known_gap', h.known_gap_cover)), '[]'::json)
               FROM ids i CROSS JOIN LATERAL hospital_profile_for(i.id, :snap, :state) h),
  'cover', (SELECT coalesce(json_agg(json_build_object('policy_id', i.id, 'category', c.category, 'cover', c.cover)), '[]'::json)
            FROM ids i CROSS JOIN LATERAL hospital_cover_for(i.id, :snap, :state) c),
  'extras', (SELECT coalesce(json_agg(json_build_object('policy_id', i.id, 'service', e.service, 'covered', e.covered,
                                                        'wait_months', e.wait_months)), '[]'::json)
             FROM ids i CROSS JOIN LATERAL extras_services_for(i.id, :snap, :state, :family) e),
  'benefits', (SELECT coalesce(json_agg(json_build_object('policy_id', i.id, 'service', b.service, 'item', b.item,
                                                          'amount', b.benefit_amount, 'pct', b.benefit_pct)), '[]'::json)
               FROM ids i CROSS JOIN LATERAL extras_benefits_for(i.id, :snap, :state, :family) b),
  'limits', (SELECT coalesce(json_agg(json_build_object(
                 'policy_id', g.policy_id, 'title', g.title, 'no_limit', g.no_benefit_limits OR g.annual_no_limit,
                 'per_person', g.limit_per_person, 'per_service', g.limit_per_service, 'per_policy', g.limit_per_policy,
                 'lifetime', g.lifetime_limit, 'count', g.service_count, 'count_years', g.service_count_years,
                 'count_type', g.service_count_type, 'free_text', g.free_text_limit,
                 'services', (SELECT json_agg(s.service) FROM extras_limit_group_services s WHERE s.group_id = g.id))), '[]'::json)
             FROM (SELECT DISTINCT ON (policy_id, title) * FROM extras_limit_groups
                   WHERE snapshot_id = :snap AND policy_id IN (SELECT id FROM ids) AND family_type = :family
                     AND dependant_scope = :scope AND state IN (:state, 'ALL')
                   ORDER BY policy_id, title, (state = :state) DESC) g),
  'rates', (SELECT coalesce(json_agg(json_build_object(
                'policy_id', i.id, 'excess_type', v.excess_type, 'excess', v.excess_option,
                'per_person', v.excess_per_person, 'per_policy', v.excess_per_policy, 'per_admission', v.excess_per_admission,
                'premium', v.premium_month, 'copayment_type', v.copayment_type, 'copay_shared', v.copayment_shared,
                'copay_private', v.copayment_private, 'copay_day_surgery', v.copayment_day_surgery,
                'ambulance', v.ambulance_cover)), '[]'::json)
            FROM ids i CROSS JOIN LATERAL variants_for(i.id, :snap, :state) v
            WHERE v.status = 'Open' AND v.family_type = :family AND v.dependant_scope = :scope),
  'closest', (SELECT coalesce(json_agg(json_build_object('comp_id', m.comp_policy_id, 'rank', m.rank,
                                                         'navy_id', m.navy_policy_id, 'navy_name', n.current_name,
                                                         'score', m.score) ORDER BY m.comp_policy_id, m.rank), '[]'::json)
              FROM closest_matches m JOIN policies n ON n.id = m.navy_policy_id
              WHERE m.snapshot_id = :snap AND m.comp_policy_id IN (SELECT id FROM ids) AND m.state = :state
                AND m.family_type = :family AND m.dependant_scope = :scope)
) AS data
"""


def fetch(db: Snapshot, *, ids: list[int], state: str, family: str, scope: str) -> dict:
    """Everything the comparison needs for these policies, in one query."""
    if not ids:
        raise EngineError("choose at least one policy")
    state, family, scope = q._state(state), q._family(family), q._scope(scope)
    clean = sorted({int(i) for i in ids})
    snap = db.live_snapshot_id()
    sql = _FETCH.replace("{ids}", "ARRAY[" + ",".join(lit(i) for i in clean) + "]")
    rows = db.rows(sql, {"snap": snap, "state": state, "family": family, "scope": scope})
    data = rows[0]["data"]
    data.update({"snapshot": snap, "state": state, "family": family, "scope": scope})
    return data


# ── values ────────────────────────────────────────────────────────────────────

def _money(v: float) -> str:
    return f"${v:,.2f}" if v != int(v) else f"${v:,.0f}"


def _excess_text(rate: dict) -> str:
    t, amount = rate["excess_type"], float(rate["excess"] or 0)
    if t in (None, "None") or amount == 0:
        return "No excess"
    if t == "PerAdmissionOnly":
        return f"{_money(amount)} each admission"
    if t == "LimitedPerPersonPerPolicy" and rate.get("per_policy"):
        return f"{_money(amount)} a person, {_money(float(rate['per_policy']))} a policy, a year"
    if t == "LimitedPerYear" and rate.get("per_policy"):
        return f"{_money(float(rate['per_admission'] or amount))} an admission, up to {_money(float(rate['per_policy']))} a year"
    return f"{_money(amount)} a person a year"


def anchor_excess(data: dict, explicit: Optional[float] = None,
                  order: Optional[list[int]] = None) -> Optional[float]:
    """The excess premiums are matched at: the one asked for, else the lowest of the first Navy Health
    policy the rep chose (in `order`, the rep's order), else of the first policy."""
    if explicit is not None:
        return float(explicit)
    known = {p["id"]: p for p in data["policies"]}
    ids = [i for i in (order or sorted(known)) if i in known]
    navy_ids = [i for i in ids if known[i]["is_navy"]] or ids
    for pid in navy_ids:
        options = sorted(float(r["excess"] or 0) for r in data["rates"] if r["policy_id"] == pid)
        if options:
            return options[0]
    return None


def chosen_rate(data: dict, policy_id: int, anchor: Optional[float]) -> Optional[dict]:
    """The rate a policy is compared at: the excess closest to the anchor (the lower on a tie), and at
    that excess the cheapest, preferring a rate without a co-payment."""
    rates = [r for r in data["rates"] if r["policy_id"] == policy_id]
    if not rates:
        return None
    target = anchor if anchor is not None else min(float(r["excess"] or 0) for r in rates)
    return min(rates, key=lambda r: (abs(float(r["excess"] or 0) - target), float(r["excess"] or 0),
                                     r["copayment_type"] not in (None, "None"), float(r["premium"])))


def values(data: dict, *, excess: Optional[float] = None,
           order: Optional[list[int]] = None) -> dict[int, dict[str, Value]]:
    """One Value per feature per policy. A feature a policy does not have is absent from its dict."""
    anchor = anchor_excess(data, excess, order)
    out: dict[int, dict[str, Value]] = {}
    for p in data["policies"]:
        pid = p["id"]
        vals: dict[str, Value] = {}
        rate = chosen_rate(data, pid, anchor)
        if rate:
            ex = float(rate["excess"] or 0)
            note = "no excess" if ex == 0 else f"at {_money(ex)} excess"
            vals["price"] = Value("price", "premium", f"{_money(float(rate['premium']))} a month",
                                  -float(rate["premium"]), float(rate["premium"]), "dollars", note)
            vals["excess"] = Value("excess", "excess", _excess_text(rate), None, ex)
            ctype = rate["copayment_type"]
            if ctype in (None, "None"):
                vals["copayment"] = Value("copayment", "copayment", "No co-payment", 0.0, 0.0)
            else:
                amount = float(rate["copay_shared"] or rate["copay_private"] or rate["copay_day_surgery"] or 0)
                vals["copayment"] = Value("copayment", "copayment",
                                          f"{_money(amount)} a day ({ctype.lower()})", -amount - 0.01, amount)
            amb = rate["ambulance"]
            vals["ambulance"] = Value("ambulance", "ambulance", AMBULANCE_LABELS.get(amb, amb),
                                      AMBULANCE_RANK.get(amb), None)

        prof = next((r for r in data["profiles"] if r["policy_id"] == pid), None)
        if prof:
            vals["tier"] = Value("tier", "tier", TIER_LABELS.get(prof["hospital_tier"], prof["hospital_tier"]),
                                 float(prof["tier_rank"]), float(prof["tier_rank"]))
            acc = prof["accommodation"]
            vals["accommodation"] = Value("accommodation", "accommodation", ACCOMMODATION_LABELS.get(acc, acc),
                                          2.0 if acc.startswith("Private") else 1.0, None)
            if prof["known_gap"] is not None:
                vals["known_gap"] = Value("known_gap", "known_gap", "Yes" if prof["known_gap"] else "No",
                                          1.0 if prof["known_gap"] else 0.0, None)
            for title in HOSPITAL_WAITING_PERIOD_TITLES:
                months = prof.get(title)
                if months is None:
                    vals[f"wait:{title}"] = Value(f"wait:{title}", "wait_months", "Not covered", None, None)
                else:
                    vals[f"wait:{title}"] = Value(f"wait:{title}", "wait_months",
                                                  "No wait" if months == 0 else f"{months} months",
                                                  -float(months), float(months), "months")

        for c in data["cover"]:
            if c["policy_id"] == pid:
                vals[f"cover:{c['category']}"] = Value(f"cover:{c['category']}", "cover_category",
                                                       COVER_LABELS[c["cover"]], float(COVER_RANK[c["cover"]]),
                                                       float(COVER_RANK[c["cover"]]))

        covered: dict[str, bool] = {}
        for e in data["extras"]:
            if e["policy_id"] == pid:
                covered[e["service"]] = bool(e["covered"])
                wait = e["wait_months"]
                text = "Not covered" if not e["covered"] else (
                    "Covered" if not wait else f"Covered, {wait} month wait")
                vals[f"extras:{e['service']}"] = Value(f"extras:{e['service']}", "extras_service", text,
                                                       1.0 if e["covered"] else 0.0, None)

        if covered:
            mine = [b for b in data["benefits"] if b["policy_id"] == pid]
            for item, (svc, _label) in EXTRAS_ITEMS.items():
                b = next((x for x in mine if x["item"] == item), None)
                key = f"item:{item}"
                if b and b["amount"] is not None:
                    vals[key] = Value(key, "extras_item", _money(float(b["amount"])), float(b["amount"]),
                                      float(b["amount"]), "dollars")
                elif b and b["pct"] is not None:
                    vals[key] = Value(key, "extras_item", f"{float(b['pct']):g}% of the fee", float(b["pct"]),
                                      float(b["pct"]), "percent")
                elif svc in covered and not covered[svc]:
                    vals[key] = Value(key, "extras_item", "Not covered", 0.0, 0.0, "any")
            groups = [g for g in data["limits"] if g["policy_id"] == pid]
            for svc in EXTRAS_SERVICES:
                if svc not in covered:
                    continue
                key = f"limit:{svc}"
                if not covered[svc]:
                    vals[key] = Value(key, "extras_limit", "Not covered", 0.0, 0.0, "any")
                    continue
                g = next((x for x in groups if svc in (x["services"] or [])), None)
                if g:
                    vals[key] = _limit_value(key, svc, g)
        out[pid] = vals
    return out


def _limit_value(key: str, svc: str, g: dict) -> Value:
    shared = [SERVICE_LABELS.get(s, s) for s in (g["services"] or []) if s != svc]
    tail = f" (shared with {', '.join(shared)})" if shared else ""
    if g["free_text"]:
        return Value(key, "extras_limit", g["free_text"], None, None, "text")
    if g["no_limit"]:
        return Value(key, "extras_limit", "No annual limit" + tail, UNLIMITED, UNLIMITED, "any")
    for field, words in (("per_person", "a person a year"), ("per_policy", "a policy a year"),
                         ("per_service", "a service")):
        if g[field] is not None:
            amount = float(g[field])
            return Value(key, "extras_limit", f"{_money(amount)} {words}{tail}", amount, amount, field)
    if g["count"]:
        years = g["count_years"] or 1
        what = "appliance" if g["count_type"] == "Appliance" else "service"
        return Value(key, "extras_limit", f"{g['count']} {what}{'s' if g['count'] > 1 else ''} every "
                     f"{years} year{'s' if years > 1 else ''}{tail}", None, None, "count")
    if g["lifetime"] is not None:
        amount = float(g["lifetime"])
        return Value(key, "extras_limit", f"{_money(amount)} lifetime{tail}", amount, amount, "lifetime")
    return Value(key, "extras_limit", "No limit listed" + tail, None, None, "text")


def label(key: str) -> str:
    f = FEATURES.get(key)
    return f.label if f else key


def describe(data: dict) -> dict[int, dict[str, Any]]:
    """Per policy: its name, fund and type, for headings."""
    return {p["id"]: p for p in data["policies"]}
