"""The type-ahead index and the benefit search: what a rep sees as they type."""
from config import features as feats
from engine.catalog import Catalog, words


def _rows():
    def row(i, name, fund, fund_name, navy=False, ptype="hospital", brand="", fund_brands="", tier="Gold",
            fund_type="Open"):
        return {"id": i, "current_name": name, "product_type": ptype, "is_navy": navy, "fund_code": fund,
                "fund_name": fund_name, "fund_type": fund_type, "brand_name": brand, "fund_brands": fund_brands,
                "tier": tier}
    return [
        row(1, "Core Silver+ Hospital", "NHB", "Navy Health Ltd", navy=True, tier="SilverPlus"),
        row(2, "Premium Gold Hospital 350", "NHB", "Navy Health Ltd", navy=True),
        row(3, "Premium Gold Hospital 750", "NHB", "Navy Health Ltd", navy=True),
        row(4, "HCF CORPORATE CARE BRONZE PLUS HOSPITAL $750 EXCESS", "HCF",
            "The Hospitals Contribution Fund of Australia", tier="BronzePlus"),
        row(5, "Frank Bronze Hospital", "GMH", "GMHBA Limited", brand="Frank Health Insurance",
            fund_brands="Frank Health Insurance", tier="Bronze"),
        row(6, "Top Hospital (Gold)", "NTF", "Teachers Health", brand="UniHealth", fund_brands="UniHealth"),
        row(7, "Top Extras", "NTF", "Teachers Health", ptype="extras", tier=None),
        row(8, "ACA Gold Hospital", "ACA", "ACA Health Benefits Fund", fund_type="Restricted"),
    ]


def cat():
    return Catalog.from_rows(_rows(), snapshot_id=1)


def test_plus_and_ampersand_read_the_same_however_they_are_written():
    assert words("Silver+") == ["silver", "plus"]
    assert words("Gold & Extras") == ["gold", "and", "extras"]


def test_every_word_must_start_a_word_of_the_policy_or_its_company():
    found = cat().search("hcf bro", side="comp")
    assert [p["id"] for p in found["policies"]] == [4]
    assert cat().search("hcf zzz", side="comp")["policies"] == []


def test_a_navy_search_only_finds_navy_policies():
    ids = [p["id"] for p in cat().search("gold", side="navy")["policies"]]
    assert ids == [2, 3]


def test_a_company_name_is_offered_as_a_company_before_its_policies():
    found = cat().search("hcf", side="comp")
    assert [f["code"] for f in found["funds"]] == ["HCF"]


def test_a_brand_finds_its_fund_and_its_policies():
    found = cat().search("frank", side="comp")
    assert [f["code"] for f in found["funds"]] == ["GMH"]
    assert [p["id"] for p in found["policies"]] == [5]
    assert found["policies"][0]["brand"] == "Frank Health Insurance"


def test_picking_a_company_keeps_the_search_to_its_policies():
    found = cat().search("", side="comp", fund="NTF")
    assert sorted(p["id"] for p in found["policies"]) == [6, 7]


def test_navy_health_is_never_offered_as_a_competitor_company():
    assert cat().search("navy", side="comp")["funds"] == []


def test_restricted_funds_are_flagged():
    found = cat().search("aca", side="comp")
    assert found["funds"][0]["restricted"] is True


def test_a_name_starting_with_the_words_ranks_first():
    found = cat().search("premium gold 750", side="navy")
    assert found["policies"][0]["id"] == 3


def test_benefit_search_finds_what_people_say():
    assert feats.search("crown")[0].key == "item:DentalMajor615FullCrownVeneered"
    assert feats.search("hip")[0].key == "cover:JointReplacements"
    assert "extras:Optical" in [f.key for f in feats.search("glasses")]
    assert "extras:Endodontic" in [f.key for f in feats.search("root canal")]
    assert feats.search("") == []


def test_every_clinical_category_extras_service_and_item_has_a_feature():
    from config.reference import CLINICAL_CATEGORIES, EXTRAS_ITEMS, EXTRAS_SERVICES
    assert all(f"cover:{c}" in feats.FEATURES for c in CLINICAL_CATEGORIES)
    assert all(f"extras:{s}" in feats.FEATURES and f"limit:{s}" in feats.FEATURES for s in EXTRAS_SERVICES)
    assert all(f"item:{i}" in feats.FEATURES for i in EXTRAS_ITEMS)


def test_every_synonym_points_at_a_real_feature():
    assert all(k in feats.FEATURES for keys in feats.SYNONYMS.values() for k in keys)
