"""The product parser, against small records shaped like the 1 Oct 2026 feed.

Each test builds the XML it needs, so the case under test is visible in the
test rather than in a fixture file. The shapes come from docs/DATA_MODEL.md
and from reading the real files; tests/test_ingest_release.py runs the whole
pipeline on a release built the same way.
"""
import json

import pytest
from lxml import etree

from config.reference import CLINICAL_CATEGORIES, EXTRAS_SERVICES
from ingest.products import FundDefaults, ProductError, Registry, parse_product

NS = 'xmlns="http://admin.privatehealth.gov.au/ws/Schemas" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
FUND = FundDefaults(ambulance_cover="EmergencyOnly", known_gap=True,
                    preferred=[{"state": "ALL", "covered": "NotCovered", "free_text": None}])


def who(adults=2, dependants=(), only_one=False):
    if only_one:
        return '<WhoIsCovered OnlyOnePerson="true"><Coverage xsi:nil="true" /></WhoIsCovered>'
    covers = "".join(f'<DependantCover Title="{t}" Covered="true" />' for t in dependants)
    return f'<WhoIsCovered OnlyOnePerson="false"><Coverage NumberOfAdults="{adults}">{covers}</Coverage></WhoIsCovered>'


def hospital_cover(tier="Gold", features="Some features.", cover="Covered"):
    categories = "".join(f'<MedicalService Title="{c}" Cover="{cover}" />' for c in CLINICAL_CATEGORIES)
    return (
        f'<HospitalCover BasedOnID=""><HospitalTier>{tier}</HospitalTier>'
        '<AgeBasedDiscount Available="false" AvailableForTransferee="false" />'
        '<Accommodation>PrivateOrPublic</Accommodation>'
        '<ProductKnownGapCover UseFund="false" KnownGapCover="true" />'
        '<HospitalPercent xsi:nil="true" />'
        f'<MedicalServices>{categories}</MedicalServices>'
        '<WaitingPeriods>'
        '<WaitingPeriod Unit="Month" Atomic="true" Title="SubAcute">2</WaitingPeriod>'
        '<WaitingPeriod Unit="Month" Atomic="true" Title="PreExisting">12</WaitingPeriod>'
        '<WaitingPeriod Unit="Month" Atomic="true" Title="PregnancyBirth">12</WaitingPeriod>'
        '<WaitingPeriod Unit="Month" Atomic="true" Title="Other">2</WaitingPeriod>'
        '</WaitingPeriods>'
        f'<OtherProductFeatures>{features}</OtherProductFeatures></HospitalCover>'
    )


def general_cover(benefits_lists="", features_on=(), services_covered=True, limits=""):
    services = "".join(
        f'<GeneralHealthService Title="{s}" Covered="{"true" if services_covered else "false"}" '
        f'HasSpecialFeatures="{"true" if s in features_on else "false"}" Atomic="true">'
        '<WaitingPeriod Unit="Month" Atomic="true">0</WaitingPeriod>'
        f'{benefits_lists}</GeneralHealthService>'
        for s in EXTRAS_SERVICES
    )
    return (
        '<GeneralHealthCover BasedOnID=""><BenefitLimits>'
        f'{limits}<NoLimitOnPreventativeDental>false</NoLimitOnPreventativeDental></BenefitLimits>'
        '<GeneralHealthServices AllStatesHaveSameBenefits="true" MaxServicesCovered="1" OptionalServicesProduct="No">'
        f'{services}<OtherServices xsi:nil="true" /></GeneralHealthServices>'
        '<ProductPreferredProviderServices UseFund="false"><PreferredProviderServices Atomic="true">'
        '<PreferredProvider State="ALL" Covered="NotSpecified" />'
        '</PreferredProviderServices></ProductPreferredProviderServices></GeneralHealthCover>'
    )


def product_xml(*, pid="I1", product_type="Hospital", state="NSW", name="Gold 500", status="Open",
                who_xml=None, hospital="", general="", excess='<Excesses ExcessType="None" Atomic="true" />',
                brands="<Brands />", bundle="<OnlyAvailableWith><NotApplicable xsi:nil=\"true\" /></OnlyAvailableWith>"):
    return (
        f'<Product {NS} ProductItemID="{pid}" ProductID="P-{pid}" ProductCode="G1/QNSW20" FundItemID="F1">'
        '<FundCode>ACA</FundCode><ProductGroupCode>G1</ProductGroupCode>'
        f'<Name>{name}</Name><ProductType>{product_type}</ProductType><ProductStatus>{status}</ProductStatus>'
        '<FundsProductCode>C-1</FundsProductCode>'
        '<DateValidFrom>2026-04-01</DateValidFrom><DateValidTo xsi:nil="true" />'
        f'<State>{state}</State>{who_xml or who()}'
        f'{excess}<CoPayments CoPaymentType="None" Atomic="true" />'
        '<MedicareLevySurchargeExempt>true</MedicareLevySurchargeExempt>'
        '<PremiumNoRebate>300.00</PremiumNoRebate><PremiumHospitalComponent>300.00</PremiumHospitalComponent>'
        f'{brands}{bundle}{hospital}{general}'
        '<ProductAmbulance UseFund="false"><Ambulance CallOutFees="false" Cover="EmergencyOnly" /></ProductAmbulance>'
        '</Product>'
    )


def parse(xml, registry=None):
    el = etree.fromstring(xml.encode("utf-8"))
    return parse_product(el, source_file="test", fund=FUND, registry=registry or Registry())


def general_body(registry, def_id):
    """A staged definition, as the promote step reads it."""
    return json.loads(registry.definitions[def_id][1])


def limit_group_bodies(registry, out):
    return [json.loads(registry.limit_groups[lg_id]) for lg_id in out["raw"]["limit_groups"]]


def test_hospital_row_is_one_definition_under_its_state():
    out = parse(product_xml(hospital=hospital_cover(tier="Gold")))
    assert out["product_type"] == "Hospital"
    assert out["gov_product_code"] == "G1/QNSW20"
    rate = out["raw"]["rate"]
    assert rate["family_type"] == "couple" and rate["dependant_scope"] == ""
    assert rate["state"] == "NSW"
    [d] = out["raw"]["definitions"]
    assert d["state"] == "NSW" and d["general"] is None and d["hospital"] is not None


def test_family_and_dependant_scope_are_derived_from_who_is_covered():
    xml = product_xml(who_xml=who(adults=2, dependants=("Student", "Child", "NonStudent")),
                      hospital=hospital_cover())
    rate = parse(xml)["raw"]["rate"]
    assert rate["family_type"] == "family"
    assert rate["dependant_scope"] == "Child+NonStudent+Student"
    assert rate["covers_adult_dependants"] is True
    assert rate["dependant_titles"] == ["Child", "NonStudent", "Student"]


def test_single_person_has_no_dependants():
    rate = parse(product_xml(who_xml=who(only_one=True), hospital=hospital_cover()))["raw"]["rate"]
    assert rate["family_type"] == "single"
    assert rate["who_covered_json"]["OnlyOnePerson"] is True


def test_an_unseen_household_shape_fails_rather_than_guessing():
    with pytest.raises(ValueError, match="unseen WhoIsCovered shape"):
        parse(product_xml(who_xml=who(adults=1), hospital=hospital_cover()))


def test_an_unknown_hospital_tier_names_the_product():
    with pytest.raises(ProductError, match="I1: unknown HospitalTier 'Platinum'"):
        parse(product_xml(hospital=hospital_cover(tier="Platinum")))


def test_free_text_is_display_only_and_does_not_change_the_definition():
    registry = Registry()
    a = parse(product_xml(pid="A", hospital=hospital_cover(features="Includes travel.")), registry)
    b = parse(product_xml(pid="B", hospital=hospital_cover(features="Includes travel!")), registry)
    assert a["raw"]["definitions"][0]["hospital"] == b["raw"]["definitions"][0]["hospital"]
    assert a["raw"]["definitions"][0]["texts"]["other_features"] == "Includes travel."
    assert b["raw"]["definitions"][0]["texts"]["other_features"] == "Includes travel!"


def test_identical_definitions_are_stored_once():
    registry = Registry()
    for pid in ("A", "B", "C"):
        parse(product_xml(pid=pid, hospital=hospital_cover()), registry)
    assert len([k for k, (kind, _) in registry.definitions.items() if kind == "hospital"]) == 1


def test_a_benefit_overridden_for_one_state_gives_that_state_its_own_definition():
    lists = (
        '<BenefitsList State="ALL" Atomic="true"><Benefit Type="Dollars" Item="PhysiotherapyInitial">40.00</Benefit>'
        '<Benefit Type="Dollars" Item="PhysiotherapySubsequent">30.00</Benefit></BenefitsList>'
        '<BenefitsList State="NSW" Atomic="true"><Benefit Type="Dollars" Item="PhysiotherapyInitial">55.00</Benefit></BenefitsList>'
    )
    registry = Registry()
    out = parse(product_xml(state="ALL", general=general_cover(benefits_lists=lists)), registry)
    by_state = {d["state"]: general_body(registry, d["general"]) for d in out["raw"]["definitions"]}
    assert set(by_state) == {"ALL", "NSW"}
    all_items = {(b["item"]): b["amount"] for b in by_state["ALL"]["benefits"] if b["service"] == "Physiotherapy"}
    nsw_items = {(b["item"]): b["amount"] for b in by_state["NSW"]["benefits"] if b["service"] == "Physiotherapy"}
    assert all_items == {"PhysiotherapyInitial": 40.0, "PhysiotherapySubsequent": 30.0}
    # The override replaces one item; the other still comes from the ALL list.
    assert nsw_items == {"PhysiotherapyInitial": 55.0, "PhysiotherapySubsequent": 30.0}


def test_a_state_row_with_an_override_for_another_state_is_refused():
    lists = '<BenefitsList State="VIC" Atomic="true"><Benefit Type="Dollars" Item="OrthoticsX">1</Benefit></BenefitsList>'
    with pytest.raises(ProductError, match="inside a NSW row"):
        parse(product_xml(state="NSW", general=general_cover(benefits_lists=lists)))


def test_an_empty_products_bundle_is_kept_as_a_requirement_not_dropped():
    bundle = '<OnlyAvailableWith Atomic="true"><Products /></OnlyAvailableWith>'
    rate = parse(product_xml(general=general_cover(), bundle=bundle))["raw"]["rate"]
    assert rate["only_available_with"] == {"kind": "Products", "group_prefixes": []}


def test_a_bundle_with_prefixes_lists_them():
    bundle = '<OnlyAvailableWith Atomic="true"><Products>I3;I4;I5</Products></OnlyAvailableWith>'
    rate = parse(product_xml(general=general_cover(), bundle=bundle))["raw"]["rate"]
    assert rate["only_available_with"] == {"kind": "Products", "group_prefixes": ["I3", "I4", "I5"]}


def test_a_closed_product_is_read_as_closed():
    out = parse(product_xml(status="Closed", hospital=hospital_cover()))
    assert out["status"] == "Closed"


def test_limit_groups_keep_the_cap_and_the_services_it_shares():
    limits = (
        '<BenefitLimit Title="DentalGeneral"><ServicesCombined IncludesOtherUnlisted="true" Atomic="true">'
        '<Service SubLimitsApply="true">DentalGeneral</Service><Service SubLimitsApply="true">DentalMajor</Service>'
        '</ServicesCombined><LimitPerPerson>1700</LimitPerPerson></BenefitLimit>'
        '<BenefitLimit Title="Optical"><ServicesCombined IncludesOtherUnlisted="false" Atomic="true">'
        '<Service SubLimitsApply="true">Optical</Service></ServicesCombined>'
        '<ServiceCountLimit ServiceLimitType="Appliance"><ServiceCount>2</ServiceCount>'
        '<ServiceCountYears>3</ServiceCountYears></ServiceCountLimit></BenefitLimit>'
        '<BenefitLimit Title="Physio"><ServicesCombined IncludesOtherUnlisted="false" Atomic="true">'
        '<Service SubLimitsApply="false">Physiotherapy</Service></ServicesCombined>'
        '<FreeTextLimit>Shared with the gym.</FreeTextLimit></BenefitLimit>'
    )
    registry = Registry()
    out = parse(product_xml(general=general_cover(limits=limits)), registry)
    groups = {g["title"]: g for g in limit_group_bodies(registry, out)}
    assert groups["DentalGeneral"]["limit_per_person"] == 1700.0
    assert [s["service"] for s in groups["DentalGeneral"]["services"]] == ["DentalGeneral", "DentalMajor"]
    assert groups["Optical"]["service_count"] == 2 and groups["Optical"]["service_count_type"] == "Appliance"
    assert groups["Physio"]["free_text_limit"] == "Shared with the gym."


def test_the_fund_resolves_products_that_use_it_for_ambulance_and_preferred_providers():
    xml = product_xml(general=general_cover()).replace(
        '<ProductAmbulance UseFund="false"><Ambulance CallOutFees="false" Cover="EmergencyOnly" /></ProductAmbulance>',
        '<ProductAmbulance UseFund="true"><Ambulance CallOutFees="false" Cover="None" /></ProductAmbulance>').replace(
        '<ProductPreferredProviderServices UseFund="false">',
        '<ProductPreferredProviderServices UseFund="true">')
    fund = FundDefaults(ambulance_cover="EmergencyAndNonEmergency", known_gap=None,
                        preferred=[{"state": "ALL", "covered": "Covered", "free_text": "Network dentists."}])
    el = etree.fromstring(xml.encode("utf-8"))
    out = parse_product(el, source_file="test", fund=fund, registry=Registry())
    assert out["raw"]["rate"]["ambulance_cover"] == "EmergencyAndNonEmergency"
    assert out["raw"]["rate"]["ambulance_use_fund"] is True
    [d] = out["raw"]["definitions"]
    assert d["texts"]["preferred_free"] == "Network dentists."


def test_every_service_is_listed_with_its_covered_flag():
    registry = Registry()
    parse(product_xml(general=general_cover(services_covered=False)), registry)
    body = json.loads(next(blob for kind, blob in registry.definitions.values() if kind == "general"))
    assert set(body["services"]) == set(EXTRAS_SERVICES)
    assert not any(s["covered"] for s in body["services"].values())
