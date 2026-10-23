"""One government <Product> element, turned into the staging record the SQL
promotes: the rate fields, the product definitions keyed by state, and the
household-scoped limit groups.

The feed repeats the whole product definition on every rate row (one row per
state x household x excess option). The product definition is what the
promote step stores once per policy; the rate fields are what it stores per
rate. This module only reads one row; deciding what is shared happens in
ingest/sql/promote.sql, where the whole snapshot is visible at once.

Every enumerated value is checked against config/reference.py here, so a
value the government adds fails the load with the product's id in the message
instead of being stored as something the query layer does not understand.
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from lxml import etree

from config.reference import (
    CLINICAL_CATEGORIES,
    HOSPITAL_TIER_RANK,
    HOSPITAL_WAITING_PERIOD_TITLES,
    STATES,
    covers_adult_dependants,
    dependant_scope,
    derive_family_type,
    excess_option,
)
from ingest.feed import boolean, child, children, integer, number, q, text

PRODUCT_TYPES = {"Hospital": "hospital", "GeneralHealth": "extras", "Combined": "combined"}
ALL = "ALL"


class ProductError(ValueError):
    """A product the parser will not guess at. The message names the product."""


class Registry:
    """Content-addressed store for the parts many rate rows share: a product's
    definition for one state, and a limit group. Each distinct body is written
    once, and a row refers to it by id. A 1 Oct 2026 Hospital row repeats its
    definition on every rate; this is what keeps the staged upload about a
    seventh of the size, and what makes the promote step's "same content"
    checks a comparison of ids."""

    def __init__(self) -> None:
        self.definitions: dict[str, tuple[str, str]] = {}   # id -> (kind, json)
        self.limit_groups: dict[str, str] = {}              # id -> json

    @staticmethod
    def _id(kind: str, body) -> tuple[str, str]:
        blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256((kind + "\n" + blob).encode("utf-8")).hexdigest()[:32]
        return digest, blob

    def definition(self, kind: str, body) -> str:
        key, blob = self._id(kind, body)
        self.definitions.setdefault(key, (kind, blob))
        return key

    def limit_group(self, body) -> str:
        key, blob = self._id("limit_group", body)
        self.limit_groups.setdefault(key, blob)
        return key


def _attr(el: Optional[etree._Element], name: str) -> Optional[str]:
    return None if el is None else el.get(name)


def _covered_titles(who: Optional[etree._Element]) -> tuple[bool, Optional[int], frozenset]:
    if who is None:
        raise ProductError("no WhoIsCovered")
    only_one = boolean(who.get("OnlyOnePerson")) or False
    cov = who.find(q("Coverage"))
    adults = integer(_attr(cov, "NumberOfAdults"))
    titles = frozenset(
        d.get("Title") for d in (cov.findall(q("DependantCover")) if cov is not None else [])
        if d.get("Covered") == "true"
    )
    return only_one, adults, titles


def _rate_who(el: etree._Element) -> dict:
    who = child(el, "WhoIsCovered")
    only_one, adults, titles = _covered_titles(who)
    family = derive_family_type(only_one_person=only_one, number_of_adults=adults,
                                covered_dependant_titles=titles)
    return {
        "family_type": family,
        "dependant_scope": dependant_scope(titles),
        "dependant_titles": sorted(titles),
        "covers_adult_dependants": covers_adult_dependants(titles),
        "covers_disability_dependants": "Disability" in titles,
        "who_covered_json": {"OnlyOnePerson": only_one, "NumberOfAdults": adults, "covered": sorted(titles)},
    }


def _excess(el: etree._Element) -> dict:
    ex = child(el, "Excesses")
    if ex is None:
        raise ProductError("no Excesses")
    etype = ex.get("ExcessType")
    per_adm = number(text(ex, "ExcessPerAdmission"))
    per_person = number(text(ex, "ExcessPerPerson"))
    per_policy = number(text(ex, "ExcessPerPolicy"))
    waivers = [w.text.strip() for w in ex.findall(q("ExcessWaivers") + "/" + q("Waiver")) if w.text]
    return {
        "excess": {"type": etype, "per_admission": per_adm, "per_person": per_person,
                   "per_policy": per_policy, "waivers": waivers},
        "excess_option": excess_option(etype, per_person, per_adm),
    }


def _copayment(el: etree._Element) -> dict:
    cp = child(el, "CoPayments")
    if cp is None:
        raise ProductError("no CoPayments")
    return {
        "type": cp.get("CoPaymentType"),
        "shared": number(text(cp, "Shared")),
        "shared_max": number(text(cp, "SharedMax")),
        "private": number(text(cp, "Private")),
        "private_max": number(text(cp, "PrivateMax")),
        "day_surgery": number(text(cp, "DaySurgery")),
        "annual_max": number(text(cp, "AnnualMax")),
    }


def _brands(el: etree._Element) -> tuple[list[str], Optional[str]]:
    codes = [b.text.strip() for b in el.findall(q("Brands") + "/" + q("BrandCode")) if b.text]
    return codes, (codes[0] if codes else None)


def _corporate(el: etree._Element) -> dict:
    c = child(el, "Corporate")
    info = child(c, "CorporateInfo")
    return {
        "is_corporate": boolean(_attr(c, "IsCorporate")) or False,
        "text": text(info, "CorporateText"),
    }


def _only_available_with(el: etree._Element) -> Optional[dict]:
    oa = child(el, "OnlyAvailableWith")
    if oa is None:
        return None
    # These are present-or-absent markers, so they count even when xsi:nil.
    if oa.find(q("NotApplicable")) is not None:
        return None
    if oa.find(q("AnyHospital")) is not None:
        return {"kind": "AnyHospital", "group_prefixes": []}
    if oa.find(q("AnyGeneralHealth")) is not None:
        return {"kind": "AnyGeneralHealth", "group_prefixes": []}
    # The schema types Products as a string, so an empty <Products /> is valid.
    # 485 products in the 1 Oct 2026 extract carry one with no prefixes listed:
    # kept as a Products requirement with an empty list, not guessed at.
    products = oa.find(q("Products"))
    if products is not None:
        names = (products.text or "").split(";")
        return {"kind": "Products", "group_prefixes": [p.strip() for p in names if p.strip()]}
    raise ProductError(f"OnlyAvailableWith has no known child: {[etree.QName(c).localname for c in oa]}")


def _ambulance(el: etree._Element, fund: "FundDefaults") -> dict:
    pa = child(el, "ProductAmbulance")
    if pa is None:
        raise ProductError("no ProductAmbulance")
    use_fund = boolean(_attr(pa, "UseFund")) or False
    amb = child(pa, "Ambulance")
    cover = amb.get("Cover") if amb is not None else None
    if use_fund:
        cover = fund.ambulance_cover
    if cover is None:
        raise ProductError("ambulance cover missing")
    return {"ambulance_cover": cover, "ambulance_use_fund": use_fund}


class FundDefaults:
    """What a product inherits from its fund when it says UseFund=true."""

    def __init__(self, ambulance_cover: Optional[str], known_gap: Optional[bool],
                 preferred: list[dict]):
        self.ambulance_cover = ambulance_cover
        self.known_gap = known_gap
        self.preferred = preferred


# ── Hospital definition ───────────────────────────────────────────────────────

def _hospital(hc: etree._Element, fund: FundDefaults, pid: str) -> dict:
    tier = text(hc, "HospitalTier")
    if tier not in HOSPITAL_TIER_RANK:
        raise ProductError(f"{pid}: unknown HospitalTier {tier!r}")
    accommodation = text(hc, "Accommodation")
    age = child(hc, "AgeBasedDiscount")
    gap = child(hc, "ProductKnownGapCover")
    known_gap = fund.known_gap if _attr(gap, "UseFund") == "true" else boolean(_attr(gap, "KnownGapCover"))

    waits: dict[str, int] = {}
    for wp in hc.findall(q("WaitingPeriods") + "/" + q("WaitingPeriod")):
        title = wp.get("Title")
        if wp.get("Unit") != "Month":
            raise ProductError(f"{pid}: hospital wait {title} unit {wp.get('Unit')!r}")
        if title not in HOSPITAL_WAITING_PERIOD_TITLES:
            raise ProductError(f"{pid}: unknown hospital wait {title!r}")
        waits[title] = int(wp.text)
    for required in ("SubAcute", "PreExisting", "Other"):
        if required not in waits:
            raise ProductError(f"{pid}: hospital wait {required} missing")

    cover: dict[str, str] = {}
    for ms in hc.findall(q("MedicalServices") + "/" + q("MedicalService")):
        title = ms.get("Title")
        if title not in CLINICAL_CATEGORIES:
            raise ProductError(f"{pid}: unknown clinical category {title!r}")
        cover[title] = ms.get("Cover")
    if len(cover) != len(CLINICAL_CATEGORIES):
        raise ProductError(f"{pid}: {len(cover)} clinical categories, expected {len(CLINICAL_CATEGORIES)}")

    return {
        "tier": tier,
        "tier_rank": HOSPITAL_TIER_RANK[tier],
        "accommodation": accommodation,
        "waits": waits,
        "age_discount_available": boolean(_attr(age, "Available")) or False,
        "age_discount_for_transferee": boolean(_attr(age, "AvailableForTransferee")) or False,
        "known_gap_cover": known_gap,
        "hospital_percent": number(text(hc, "HospitalPercent")),
        "limit_hospital_days": integer(text(hc, "LimitHospitalDays")),
        "cover": cover,
    }


# ── General (extras) definition ───────────────────────────────────────────────

def _benefit_entries(gh: etree._Element, pid: str) -> list[dict]:
    entries = []
    for svc in gh.findall(q("GeneralHealthServices") + "/" + q("GeneralHealthService")):
        title = svc.get("Title")
        for bl in svc.findall(q("BenefitsList")):
            state = bl.get("State")
            for b in bl.findall(q("Benefit")):
                if b.text is None:
                    continue
                dollars = b.get("Type") == "Dollars"
                if not dollars and b.get("Type") != "Percent":
                    raise ProductError(f"{pid}: benefit type {b.get('Type')!r}")
                entries.append({
                    "state": state, "service": title, "item": b.get("Item"),
                    "amount": float(b.text) if dollars else None,
                    "pct": None if dollars else float(b.text),
                })
    return entries


def _effective_benefits(entries: list[dict], key: str) -> list[dict]:
    """The benefits a key (state) sees: its own lists, falling back per item to
    the ALL lists. Only per item, never per list, so a state override of one
    item does not hide the others."""
    merged: dict[tuple, dict] = {}
    for e in entries:
        if e["state"] == ALL:
            merged[(e["service"], e["item"])] = e
    for e in entries:
        if e["state"] == key and key != ALL:
            merged[(e["service"], e["item"])] = e
    return [
        {"service": s, "item": i, "amount": e["amount"], "pct": e["pct"]}
        for (s, i), e in sorted(merged.items())
    ]


def _effective_preferred(entries: list[dict], key: str) -> dict:
    own = next((e for e in entries if e["state"] == key), None)
    default = next((e for e in entries if e["state"] == ALL), None)
    pick = own or default
    if pick is None:
        return {"covered": "NotSpecified", "free_text": None}
    return {"covered": pick["covered"], "free_text": pick["free_text"]}


def _preferred_entries(gh: etree._Element, fund: FundDefaults, pid: str) -> list[dict]:
    ppps = child(gh, "ProductPreferredProviderServices")
    if ppps is not None and ppps.get("UseFund") == "true":
        return fund.preferred
    pps = child(ppps, "PreferredProviderServices")
    out = []
    for p in children(pps, "PreferredProvider"):
        out.append({"state": p.get("State"), "covered": p.get("Covered"), "free_text": text(p, "FreeText")})
    return out


def _services(gh: etree._Element, pid: str) -> dict:
    services: dict[str, dict] = {}
    for svc in gh.findall(q("GeneralHealthServices") + "/" + q("GeneralHealthService")):
        title = svc.get("Title")
        wp = child(svc, "WaitingPeriod")
        if wp is not None and wp.get("Unit") != "Month":
            raise ProductError(f"{pid}: service {title} wait unit {wp.get('Unit')!r}")
        services[title] = {
            "covered": svc.get("Covered") == "true",
            "wait_months": integer(wp.text) if wp is not None else None,
        }
    return services


def _services_special(gh: etree._Element) -> dict[str, bool]:
    return {
        svc.get("Title"): svc.get("HasSpecialFeatures") == "true"
        for svc in gh.findall(q("GeneralHealthServices") + "/" + q("GeneralHealthService"))
    }


def _general_bundle(gh: etree._Element, fund: FundDefaults, pid: str) -> dict:
    """The parts of the general definition that are per product (not per
    state). Benefits and preferred providers are split by state afterwards."""
    ghs = child(gh, "GeneralHealthServices")
    bl_root = child(gh, "BenefitLimits")
    return {
        # Yes/No in this feed, not true/false: the one flag written that way.
        "optional_services": _attr(ghs, "OptionalServicesProduct") == "Yes",
        "max_services_covered": integer(_attr(ghs, "MaxServicesCovered")),
        "all_states_same": boolean(_attr(ghs, "AllStatesHaveSameBenefits")),
        "no_limit_preventative_dental": boolean(text(bl_root, "NoLimitOnPreventativeDental")) or False,
        "other_services_text": text(ghs, "OtherServices"),
        "special": sorted(t for t, svc in _services_special(gh).items() if svc),
        "services": _services(gh, pid),
        "benefit_entries": _benefit_entries(gh, pid),
        "preferred_entries": _preferred_entries(gh, fund, pid),
    }


def _general_for_key(bundle: dict, key: str) -> dict:
    """The part of the extras definition that identifies it. Free text and the
    special-features flags are display, carried beside it (see _texts)."""
    return {
        "optional_services": bundle["optional_services"],
        "max_services_covered": bundle["max_services_covered"],
        "all_states_same": bundle["all_states_same"],
        "no_limit_preventative_dental": bundle["no_limit_preventative_dental"],
        "preferred_provider": {"covered": _effective_preferred(bundle["preferred_entries"], key)["covered"]},
        "services": bundle["services"],
        "benefits": _effective_benefits(bundle["benefit_entries"], key),
    }


def _definitions(row_state: str, hospital: Optional[dict], bundle: Optional[dict], pid: str,
                 other_features: Optional[str]) -> list[dict]:
    """The product definition for each state key the row speaks for.

    A row for one state speaks for that state. A row for ALL speaks for ALL,
    and for any state that a benefit list overrides, it also gives that
    state's full definition (its overrides applied over the ALL lists). The
    hospital side never varies inside a row, so it goes with the row's own key.
    """
    if row_state != ALL and row_state not in STATES:
        raise ProductError(f"{pid}: unknown State {row_state!r}")
    if bundle is None:
        return [{"state": row_state, "hospital": hospital, "general": None,
                 "texts": {"other_features": other_features}}]

    list_states = {e["state"] for e in bundle["benefit_entries"]} - {ALL}
    unknown = list_states - set(STATES)
    if unknown:
        raise ProductError(f"{pid}: benefit list for unknown state {sorted(unknown)}")
    keys = [row_state]
    if row_state == ALL:
        keys += sorted(list_states)
    elif list_states - {row_state}:
        raise ProductError(f"{pid}: benefit list for {sorted(list_states - {row_state})} inside a {row_state} row")

    out = []
    for key in keys:
        out.append({
            "state": key,
            "hospital": hospital if key == row_state else None,
            "general": _general_for_key(bundle, key),
            "texts": {
                "other_features": other_features if key == row_state else None,
                "other_services": bundle["other_services_text"],
                "preferred_free": _effective_preferred(bundle["preferred_entries"], key)["free_text"],
                "special": bundle["special"],
            },
        })
    return out


# ── Limit groups (household-scoped, not state-scoped) ─────────────────────────

def _limit_groups(gh: etree._Element, pid: str) -> list[dict]:
    bl_root = child(gh, "BenefitLimits")
    if bl_root is None:
        return []
    groups = []
    seen = set()
    for bl in bl_root.findall(q("BenefitLimit")):
        title = bl.get("Title")
        if title in seen:
            raise ProductError(f"{pid}: BenefitLimit {title} twice in one product")
        seen.add(title)
        sc = child(bl, "ServiceCountLimit")
        combined = child(bl, "ServicesCombined")
        services = [
            {
                "service": svc.text.strip(),
                "sub_limits_apply": svc.get("SubLimitsApply") == "true",
                "ind_lifetime_limit": number(svc.get("IndLifetimeLimit")),
            }
            for svc in children(combined, "Service")
        ]
        groups.append({
            "title": title,
            "no_benefit_limits": boolean(bl.get("NoBenefitLimits")) or False,
            "annual_no_limit": child(bl, "AnnualLimit") is not None,
            "limit_per_person": number(text(bl, "LimitPerPerson")),
            "limit_per_service": number(text(bl, "LimitPerService")),
            "limit_per_policy": number(text(bl, "LimitPerPolicy")),
            "lifetime_limit": number(text(bl, "LifetimeLimit")),
            "service_count": integer(text(sc, "ServiceCount")) if sc is not None else None,
            "service_count_years": integer(text(sc, "ServiceCountYears")) if sc is not None else None,
            "service_count_type": sc.get("ServiceLimitType") if sc is not None else None,
            "free_text_limit": text(bl, "FreeTextLimit"),
            "services": services,
        })
    return groups


# ── Bundling and the whole row ────────────────────────────────────────────────

def parse_product(el: etree._Element, *, source_file: str, fund: FundDefaults, registry: Registry) -> dict:
    """One <Product> as a staging record: the columns of stg_products, with the
    rest of the row in `raw`."""
    pid = el.get("ProductItemID")
    product_type = text(el, "ProductType")
    if product_type not in PRODUCT_TYPES:
        raise ProductError(f"{pid}: unknown ProductType {product_type!r}")
    status = text(el, "ProductStatus")
    if status not in ("Open", "Closed"):
        raise ProductError(f"{pid}: unknown ProductStatus {status!r}")
    state = text(el, "State")
    name = text(el, "Name")
    brand_codes, brand_code = _brands(el)
    premium = text(el, "PremiumNoRebate")

    rate = {
        "state": state,
        "gov_product_group_code": text(el, "ProductGroupCode"),
        "gov_funds_product_code": text(el, "FundsProductCode"),
        "brand_codes": brand_codes,
        "brand_code": brand_code,
        **{"is_corporate": _corporate(el)["is_corporate"], "corporate_text": _corporate(el)["text"]},
        "only_available_with": _only_available_with(el),
        "premium_hospital_component": number(text(el, "PremiumHospitalComponent")),
        "mls_exempt": boolean(text(el, "MedicareLevySurchargeExempt")),
        "valid_from": text(el, "DateValidFrom"),
        "valid_to": text(el, "DateValidTo"),
        **_rate_who(el),
        **_excess(el),
        "copayment": _copayment(el),
        **_ambulance(el, fund),
    }
    hc = child(el, "HospitalCover")
    gh = child(el, "GeneralHealthCover")
    hospital = _hospital(hc, fund, pid) if hc is not None else None
    bundle = _general_bundle(gh, fund, pid) if gh is not None else None
    if hospital is None and bundle is None:
        raise ProductError(f"{pid}: neither HospitalCover nor GeneralHealthCover")

    other_features = text(hc, "OtherProductFeatures") if hc is not None else None
    definitions = _definitions(state, hospital, bundle, pid, other_features)
    refs = [
        {
            "state": d["state"],
            "hospital": registry.definition("hospital", d["hospital"]) if d["hospital"] else None,
            "general": registry.definition("general", d["general"]) if d["general"] else None,
            "texts": d["texts"],
        }
        for d in definitions
    ]
    limit_groups = [registry.limit_group(g) for g in (_limit_groups(gh, pid) if gh is not None else [])]

    raw = {"rate": rate, "definitions": refs, "limit_groups": limit_groups}
    return {
        "gov_product_item_id": pid,
        "gov_product_id": el.get("ProductID"),
        "gov_product_code": el.get("ProductCode"),
        "fund_code": text(el, "FundCode"),
        "product_type": product_type,
        "status": status,
        "name": name,
        "state": state,
        "premium_month": premium,
        "source_file": source_file,
        "raw": raw,
    }
