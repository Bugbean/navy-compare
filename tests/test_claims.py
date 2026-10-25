"""The claim detail a rep can read aloud for one extras service (engine/claims.py): what it pays per
item and the annual cap, grouped the way a sales conversation goes rather than flat and ranked."""
from engine import claims as cl
from engine.benefits import Value


def test_services_in_finds_the_service_behind_extras_limit_and_item_keys():
    assert cl.services_in(["extras:DentalGeneral", "limit:DentalGeneral", "item:PhysiotherapyInitial"]) \
        == ["DentalGeneral", "Physiotherapy"]


def test_services_in_ignores_keys_with_no_service():
    assert cl.services_in(["price", "cover:BackNeckSpine", "wait:PreExisting", "ambulance"]) == []


def test_services_in_keeps_the_order_first_mentioned_and_drops_duplicates():
    assert cl.services_in(["limit:Physiotherapy", "extras:Physiotherapy", "extras:DentalGeneral"]) \
        == ["Physiotherapy", "DentalGeneral"]


def _vals():
    navy = {
        "extras:DentalGeneral": Value("extras:DentalGeneral", "extras_service", "Covered, 2 month wait", 1.0, None),
        "limit:DentalGeneral": Value("limit:DentalGeneral", "extras_limit", "$500 a person a year", 500.0, 500.0, "per_person"),
        "item:DentalGeneral012PeriodicExam": Value("item:DentalGeneral012PeriodicExam", "extras_item", "$28", 28.0, 28.0, "dollars"),
        "item:DentalGeneral114ScaleClean": Value("item:DentalGeneral114ScaleClean", "extras_item", "$50", 50.0, 50.0, "dollars"),
        "item:DentalGeneral121Fluoride": Value("item:DentalGeneral121Fluoride", "extras_item", "$15.60", 15.6, 15.6, "dollars"),
        "item:DentalGeneral322Extraction": Value("item:DentalGeneral322Extraction", "extras_item", "Not covered", 0.0, 0.0, "any"),
    }
    comp = {
        "extras:DentalGeneral": Value("extras:DentalGeneral", "extras_service", "Covered, 2 month wait", 1.0, None),
        "limit:DentalGeneral": Value("limit:DentalGeneral", "extras_limit", "$500 a person a year", 500.0, 500.0, "per_person"),
        "item:DentalGeneral012PeriodicExam": Value("item:DentalGeneral012PeriodicExam", "extras_item", "$32.40", 32.4, 32.4, "dollars"),
        "item:DentalGeneral114ScaleClean": Value("item:DentalGeneral114ScaleClean", "extras_item", "$61.80", 61.8, 61.8, "dollars"),
        "item:DentalGeneral121Fluoride": Value("item:DentalGeneral121Fluoride", "extras_item", "$17.80", 17.8, 17.8, "dollars"),
        "item:DentalGeneral322Extraction": Value("item:DentalGeneral322Extraction", "extras_item", "$116", 116.0, 116.0, "dollars"),
    }
    return {1: navy, 2: comp}


def test_a_block_combines_coverage_items_and_the_limit_for_one_service():
    blocks = cl.claim_groups(1, 2, _vals(), services=["DentalGeneral"])
    assert len(blocks) == 1
    b = blocks[0]
    assert b.service == "DentalGeneral" and b.label == "General dental"
    assert b.coverage.direction == "same"   # both covered, same wait
    assert b.limit.direction == "same"
    assert {i.key for i in b.items} == {
        "item:DentalGeneral012PeriodicExam", "item:DentalGeneral114ScaleClean",
        "item:DentalGeneral121Fluoride", "item:DentalGeneral322Extraction"}


def test_items_are_ranked_by_how_much_they_differ_not_alphabetically():
    blocks = cl.claim_groups(1, 2, _vals(), services=["DentalGeneral"])
    # The extraction (not covered vs $116) is the biggest, most material gap: it leads.
    assert blocks[0].items[0].key == "item:DentalGeneral322Extraction"
    assert blocks[0].items[0].direction == "comp_better"


def test_more_than_the_shown_cap_is_counted_not_dropped_silently(monkeypatch):
    monkeypatch.setattr(cl, "MAX_ITEMS_SHOWN", 2)
    blocks = cl.claim_groups(1, 2, _vals(), services=["DentalGeneral"])
    assert len(blocks[0].items) == 2 and blocks[0].more == 2


def test_a_service_with_no_data_on_either_side_is_left_out():
    blocks = cl.claim_groups(1, 2, _vals(), services=["DentalGeneral", "Physiotherapy"])
    assert [b.service for b in blocks] == ["DentalGeneral"]


def test_coverage_on_one_side_only_is_a_real_win_not_hidden_as_a_tie():
    vals = _vals()
    vals[2].pop("extras:DentalGeneral")
    vals[1]["extras:DentalGeneral"] = Value("extras:DentalGeneral", "extras_service", "Covered", 1.0, None)
    blocks = cl.claim_groups(1, 2, vals, services=["DentalGeneral"])
    assert blocks[0].coverage.direction == "missing"


def test_categories_in_finds_cover_keys_and_ignores_everything_else():
    assert cl.categories_in(["cover:BoneJointMuscle", "price", "extras:Physiotherapy", "cover:HeartVascular"]) \
        == ["cover:BoneJointMuscle", "cover:HeartVascular"]
    assert cl.categories_in(["price", "wait:Other"]) == []


def _hospital_vals():
    navy = {
        "cover:BoneJointMuscle": Value("cover:BoneJointMuscle", "cover_category", "Covered", 2.0, 2.0),
        "excess": Value("excess", "excess", "$500", 500.0, 500.0),
        "copayment": Value("copayment", "copayment", "None", 0.0, 0.0),
        "known_gap": Value("known_gap", "known_gap", "Yes", 1.0, None),
        "wait:PreExisting": Value("wait:PreExisting", "wait_months", "12 months", -12.0, 12.0, "months"),
        "wait:Other": Value("wait:Other", "wait_months", "2 months", -2.0, 2.0, "months"),
    }
    comp = {
        "cover:BoneJointMuscle": Value("cover:BoneJointMuscle", "cover_category", "Not covered", 0.0, 0.0),
        "excess": Value("excess", "excess", "$750", 750.0, 750.0),
        "copayment": Value("copayment", "copayment", "None", 0.0, 0.0),
        "known_gap": Value("known_gap", "known_gap", "No", 0.0, None),
        "wait:PreExisting": Value("wait:PreExisting", "wait_months", "12 months", -12.0, 12.0, "months"),
        "wait:Other": Value("wait:Other", "wait_months", "2 months", -2.0, 2.0, "months"),
    }
    return {1: navy, 2: comp}


def test_hospital_context_covers_what_applies_on_admission_not_a_claim_amount():
    out = cl.hospital_context(1, 2, _hospital_vals())
    assert {d.key for d in out} == {"excess", "copayment", "known_gap", "wait:PreExisting", "wait:Other"}
    excess = next(d for d in out if d.key == "excess")
    assert excess.direction == "trade_off"   # excess is never scored as a plain win
    gap = next(d for d in out if d.key == "known_gap")
    assert gap.direction == "navy_better"


def test_hospital_context_is_empty_when_neither_side_has_any_of_it():
    assert cl.hospital_context(1, 2, {1: {}, 2: {}}) == []


def test_hospital_context_skips_a_key_only_if_both_sides_lack_it():
    vals = _hospital_vals()
    del vals[2]["known_gap"]
    out = cl.hospital_context(1, 2, vals)
    gap = next(d for d in out if d.key == "known_gap")
    assert gap.direction == "missing"
