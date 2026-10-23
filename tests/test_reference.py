"""The government-fixed reference lists are internally consistent, and
family_type derivation matches every real shape found in the 1 Oct 2026
PHOL extract (see docs/DATA_MODEL.md)."""
import pytest

from config import reference as ref


def test_the_counts_the_schema_promises():
    assert len(ref.CLINICAL_CATEGORIES) == 38
    assert len(ref.EXTRAS_SERVICES) == 27
    assert len(ref.HOSPITAL_WAITING_PERIOD_TITLES) == 4


def test_no_overlap_between_the_two_taxonomies():
    """Hospital's 38 clinical categories and extras' 27 services are two
    different government taxonomies — a name should not appear in both,
    or ingest code reading the wrong one would silently validate."""
    assert not set(ref.CLINICAL_CATEGORIES) & set(ref.EXTRAS_SERVICES)


def test_every_hospital_tier_has_a_rank():
    for tier in ("Basic", "BasicPlus", "Bronze", "BronzePlus", "Silver", "SilverPlus", "Gold"):
        assert tier in ref.HOSPITAL_TIER_RANK


def test_plus_tiers_rank_between_their_base_and_the_next_tier_up():
    r = ref.HOSPITAL_TIER_RANK
    assert r["Basic"] < r["BasicPlus"] < r["Bronze"] < r["BronzePlus"] < r["Silver"] < r["SilverPlus"] < r["Gold"]


# ── family_type derivation ───────────────────────────────────────────────────
# Each case is a real WhoIsCovered shape found in the Hospital Open extract,
# named in config/reference.py's docstring by row count.

def test_only_one_person_is_single():
    assert ref.derive_family_type(only_one_person=True, number_of_adults=None,
                                  covered_dependant_titles=frozenset()) == "single"


def test_two_adults_no_dependants_is_couple():
    assert ref.derive_family_type(only_one_person=False, number_of_adults=2,
                                  covered_dependant_titles=frozenset()) == "couple"


def test_two_adults_with_dependants_is_family():
    assert ref.derive_family_type(only_one_person=False, number_of_adults=2,
                                  covered_dependant_titles=frozenset({"Child", "Student"})) == "family"


def test_one_adult_with_dependants_is_single_parent():
    assert ref.derive_family_type(only_one_person=False, number_of_adults=1,
                                  covered_dependant_titles=frozenset({"Child"})) == "single_parent"


def test_zero_adults_is_dependants_only():
    """127 rows in the 1 Oct 2026 Hospital Open extract: child-only cover."""
    assert ref.derive_family_type(only_one_person=False, number_of_adults=0,
                                  covered_dependant_titles=frozenset({"Child", "Student"})) == "dependants_only"


def test_one_adult_no_dependants_is_an_unseen_shape_not_a_guess():
    """Never seen in the real extract; refuse rather than invent a 6th category."""
    with pytest.raises(ValueError, match="unseen"):
        ref.derive_family_type(only_one_person=False, number_of_adults=1,
                               covered_dependant_titles=frozenset())


# ── dependant scope: the rate dimension family_type alone loses ──────────────

def test_dependant_scope_is_canonical_regardless_of_input_order():
    a = ref.dependant_scope(frozenset({"Student", "Child", "NonClassified"}))
    b = ref.dependant_scope(frozenset({"NonClassified", "Child", "Student"}))
    assert a == b == "Child+NonClassified+Student"
    assert ref.dependant_scope(frozenset()) == ""


def test_dependant_scope_refuses_a_title_the_schema_does_not_define():
    with pytest.raises(ValueError, match="unknown"):
        ref.dependant_scope(frozenset({"Child", "Grandparent"}))


def test_the_standard_and_extended_family_rates_are_told_apart():
    """NTF Top Hospital 500 (Gold), NSW, family: $504.42 vs $655.75 under one
    name. The only difference is NonStudent in the covered set."""
    standard = frozenset({"Child", "NonClassified", "Student"})
    extended = standard | {"NonStudent"}
    assert ref.derive_family_type(only_one_person=False, number_of_adults=2, covered_dependant_titles=standard) == "family"
    assert ref.derive_family_type(only_one_person=False, number_of_adults=2, covered_dependant_titles=extended) == "family"
    assert ref.dependant_scope(standard) != ref.dependant_scope(extended)
    assert not ref.covers_adult_dependants(standard)
    assert ref.covers_adult_dependants(extended)
    assert ref.covers_adult_dependants(frozenset({"Child", "ConditionalNonStudent"}))


# ── excess option: what a rep picks by ───────────────────────────────────────

@pytest.mark.parametrize("etype, pp, adm, expected", [
    ("None", None, None, 0.0),
    ("LimitedPerPersonPerPolicy", "500", "500", 500.0),   # per_policy may be 1000 for a couple; option stays 500
    ("LimitedPerPerson", "750", None, 750.0),
    ("PerAdmissionOnly", None, "250", 250.0),
    ("LimitedPerYear", None, "300", 300.0),
])
def test_excess_option(etype, pp, adm, expected):
    assert ref.excess_option(etype, pp, adm) == expected


def test_every_extras_item_belongs_to_a_real_service():
    assert len(ref.EXTRAS_ITEMS) == 48
    for item, (service, label) in ref.EXTRAS_ITEMS.items():
        assert service in ref.EXTRAS_SERVICES, item
        assert label


def test_a_disability_only_dependant_combination_is_still_single_parent_or_family():
    """24 rows each in the real extract: Disability alone, no Child/Student —
    still just 'has a dependant', not a 6th family_type."""
    assert ref.derive_family_type(only_one_person=False, number_of_adults=1,
                                  covered_dependant_titles=frozenset({"Disability"})) == "single_parent"
    assert ref.derive_family_type(only_one_person=False, number_of_adults=2,
                                  covered_dependant_titles=frozenset({"Disability"})) == "family"
