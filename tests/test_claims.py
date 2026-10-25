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
