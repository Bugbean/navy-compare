"""The Funds file, and the agreement-hospital and change CSVs.

A fund is small (34 records, 230 KB), so it is read whole. What a product
inherits from its fund (UseFund on ambulance, known-gap cover and preferred
providers) is returned as FundDefaults, which the product parser needs before
it can resolve those values.
"""
from __future__ import annotations

import csv

from ingest.feed import boolean, child, children, iter_elements, q, text, to_dict
from ingest.products import FundDefaults


def parse_fund(el) -> dict:
    fund_code = text(el, "FundCode")
    restrictions = child(el, "Restrictions")
    addr = child(el, "Address")
    policy_options = {
        o.get("Title"): boolean((o.text or "").strip() or None)
        for o in children(child(el, "FundPolicyOptions"), "Option")
    }
    dependants = child(el, "FundDependants")
    limits = [
        {
            "title": d.get("Title"),
            "supported": boolean(d.get("Supported")),
            "min_age": None if d.get("MinAge") is None else int(d.get("MinAge")),
            "max_age": None if d.get("MaxAge") is None else int(d.get("MaxAge")),
        }
        for d in (dependants.findall(q("DependantLimits") + "/" + q("DependantLimit")) if dependants is not None else [])
    ]
    brands = [
        {
            "brand_code": text(b, "BrandCode"),
            "name": text(b, "BrandName"),
            "use_to_brand_phis": b.get("UseToBrandPHIS") == "true",
        }
        for b in el.findall(q("RelatedBrandNames") + "/" + q("Brand"))
    ]
    amb = child(el, "Ambulance")
    preferred = [
        {"state": p.get("State"), "covered": p.get("Covered"), "free_text": text(p, "FreeText")}
        for p in children(child(el, "PreferredProviderServices"), "PreferredProvider")
    ]
    optional = [
        {"title": c.get("Title"), "covered": c.get("Covered") == "true"}
        for c in el.findall(q("FundOptionalCoverage") + "/" + q("Coverage"))
    ]
    return {
        "fund_code": fund_code,
        "fund_item_id": el.get("FundItemID"),
        "name": text(el, "FundName"),
        "fund_type": text(el, "FundType"),
        "description": text(el, "FundDescription"),
        "phone": text(el, "Phone"),
        "email": text(el, "Email"),
        "website": text(el, "Website"),
        "corporate_structure": text(el, "CorporateStructure"),
        "restriction_hint": text(restrictions, "RestrictionHint"),
        "restriction_details": text(restrictions, "RestrictionDetails"),
        "address": {
            "line1": text(addr, "AddressLine1"), "line2": text(addr, "AddressLine2"),
            "town": text(addr, "Town"), "state": text(addr, "State"), "postcode": text(addr, "Postcode"),
        } if addr is not None else None,
        "states": [s.text.strip() for s in el.findall(q("States") + "/" + q("State")) if s.text],
        "ambulance": to_dict(amb) if amb is not None else None,
        "preferred_providers": preferred,
        "dependant_limits": limits,
        "optional_coverage": optional,
        "policy_options": policy_options,
        "brands": [b for b in brands if b["brand_code"]],
    }


def read_funds(path: str) -> tuple[list[dict], dict[str, FundDefaults]]:
    """Every fund record, and the per-fund defaults the product parser needs."""
    records = [parse_fund(el) for el in iter_elements(path, "Fund")]
    defaults = {}
    for r in records:
        amb = r["ambulance"] or {}
        defaults[r["fund_code"]] = FundDefaults(
            ambulance_cover=amb.get("Cover") if isinstance(amb, dict) else None,
            known_gap=r["policy_options"].get("KnownGapCover"),
            preferred=[{"state": p["state"], "covered": p["covered"], "free_text": p["free_text"]}
                       for p in r["preferred_providers"]],
        )
    return records, defaults


AGREEMENT_FIXED_COLUMNS = ("Provider", "Title", "Address1", "Address2", "Town", "State",
                           "Postcode", "Latitude", "Longitude", "IsDay")


def read_agreement_hospitals(path: str) -> list[dict]:
    """One record per agreement hospital: its place, and the fund codes it has
    an agreement with (the 1-valued columns after the fixed ones)."""
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fund_columns = [c for c in reader.fieldnames if c not in AGREEMENT_FIXED_COLUMNS]
        for r in reader:
            rows.append({
                "provider_code": r["Provider"].strip(),
                "title": r["Title"].strip(),
                "address_line1": r["Address1"].strip() or None,
                "address_line2": r["Address2"].strip() or None,
                "town": r["Town"].strip() or None,
                "state": r["State"].strip(),
                "postcode": r["Postcode"].strip() or None,
                "latitude": r["Latitude"].strip() or None,
                "longitude": r["Longitude"].strip() or None,
                "is_day": r["IsDay"].strip().lower() == "true",
                "funds": [c for c in fund_columns if r[c].strip() == "1"],
            })
    return rows


def count_change_rows(path: str) -> dict[str, int]:
    """How many rows of each change type the publisher's CSV lists. Kept as a
    reconciliation figure in the snapshot's record counts, so the diff we
    compute can be checked against what the publisher says changed."""
    counts: dict[str, int] = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            key = r["Change Type"].strip()
            counts[key] = counts.get(key, 0) + 1
    return counts
