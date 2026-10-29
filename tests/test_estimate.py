"""engine/estimate.py: what a policy pays back in a year for a usage profile. Every case is small enough
to work out by hand, so each assertion is the arithmetic a rep could check on a calculator."""
from engine import estimate as est
from engine.estimate import Use

FEES = {"PhysiotherapySubsequent": {"fee": 100}, "OpticalSingleVisionLenses": {"fee": 250},
        "DentalGeneral012PeriodicExam": {"fee": 60}, "HearingAid": {"fee": 3000}}


def _data(*, extras, benefits, limits):
    """The shape engine/benefits.fetch returns, for policy 1 only."""
    return {
        "extras": [{"policy_id": 1, "service": s, "covered": c, "wait_months": w} for s, c, w in extras],
        "benefits": [{"policy_id": 1, "service": "", "item": i, "amount": a, "pct": p} for i, a, p in benefits],
        "limits": [{"policy_id": 1, "title": t, "no_limit": False, "per_person": None, "per_service": None,
                    "per_policy": None, "lifetime": None, "count": None, "count_years": None, "count_type": None,
                    "free_text": None, **g} for t, g in limits],
    }


def test_a_dollar_benefit_is_paid_each_time_until_the_annual_limit():
    data = _data(extras=[("Physiotherapy", True, 0)], benefits=[("PhysiotherapySubsequent", 40, None)],
                 limits=[("physio", {"services": ["Physiotherapy"], "per_person": 300})])
    pe = est.estimate(data, 1, [Use("PhysiotherapySubsequent", 10)], FEES)
    assert pe.total == 300                       # 10 x $40 = $400, capped at $300
    assert "annual limit" in pe.lines[0].note
    pe = est.estimate(data, 1, [Use("PhysiotherapySubsequent", 5)], FEES)
    assert pe.total == 200 and pe.lines[0].note == ""


def test_a_per_person_limit_applies_to_each_person_separately():
    data = _data(extras=[("Physiotherapy", True, 0)], benefits=[("PhysiotherapySubsequent", 40, None)],
                 limits=[("physio", {"services": ["Physiotherapy"], "per_person": 300})])
    pe = est.estimate(data, 1, [Use("PhysiotherapySubsequent", 10, people=2)], FEES)
    assert pe.total == 600                       # each of the two reaches their own $300


def test_a_per_policy_limit_is_shared_by_the_whole_household():
    data = _data(extras=[("Physiotherapy", True, 0)], benefits=[("PhysiotherapySubsequent", 40, None)],
                 limits=[("physio", {"services": ["Physiotherapy"], "per_person": 300, "per_policy": 500})])
    pe = est.estimate(data, 1, [Use("PhysiotherapySubsequent", 10, people=2)], FEES)
    assert pe.total == 500


def test_services_sharing_one_limit_draw_it_down_together():
    data = _data(extras=[("Physiotherapy", True, 0), ("Optical", True, 0)],
                 benefits=[("PhysiotherapySubsequent", 40, None), ("OpticalSingleVisionLenses", 200, None)],
                 limits=[("combined", {"services": ["Physiotherapy", "Optical"], "per_person": 300})])
    pe = est.estimate(data, 1, [Use("PhysiotherapySubsequent", 5), Use("OpticalSingleVisionLenses", 1)], FEES)
    assert pe.total == 300                       # $200 + $200 asked of one $300 limit
    assert round(sum(ln.paid for ln in pe.lines), 2) == 300


def test_a_percentage_benefit_is_taken_of_the_typical_fee_and_says_so():
    data = _data(extras=[("Physiotherapy", True, 0)], benefits=[("PhysiotherapySubsequent", None, 60)],
                 limits=[("physio", {"services": ["Physiotherapy"], "per_person": 1000})])
    pe = est.estimate(data, 1, [Use("PhysiotherapySubsequent", 4)], FEES)
    assert pe.total == 240                       # 60% of $100, four times
    assert "60% of a $100 fee" in pe.lines[0].note
    pe = est.estimate(data, 1, [Use("PhysiotherapySubsequent", 4, fee=150)], FEES)
    assert pe.total == 360                       # the rep's own fee wins over the typical one


def test_a_percentage_benefit_with_no_fee_to_go_on_is_not_guessed():
    data = _data(extras=[("Physiotherapy", True, 0)], benefits=[("PhysiotherapySubsequent", None, 60)],
                 limits=[])
    pe = est.estimate(data, 1, [Use("PhysiotherapySubsequent", 4)], {})
    assert pe.total == 0 and "no typical fee" in pe.lines[0].note


def test_a_dollar_benefit_never_pays_more_than_the_visit_costs():
    data = _data(extras=[("DentalGeneral", True, 0)], benefits=[("DentalGeneral012PeriodicExam", 80, None)],
                 limits=[("dental", {"services": ["DentalGeneral"], "no_limit": True})])
    pe = est.estimate(data, 1, [Use("DentalGeneral012PeriodicExam", 2)], FEES)
    assert pe.total == 120                       # $80 benefit on a $60 fee pays $60


def test_not_covered_pays_nothing_and_says_why():
    data = _data(extras=[("Physiotherapy", False, None)], benefits=[], limits=[])
    pe = est.estimate(data, 1, [Use("PhysiotherapySubsequent", 4)], FEES)
    assert pe.total == 0 and pe.lines[0].note == "not covered"
    pe = est.estimate(_data(extras=[], benefits=[], limits=[]), 1, [Use("PhysiotherapySubsequent", 4)], FEES)
    assert pe.total == 0 and pe.lines[0].note == "not covered"


def test_a_count_limit_caps_how_many_times_it_is_paid():
    data = _data(extras=[("HearingAids", True, 0)], benefits=[("HearingAid", 1000, None)],
                 limits=[("hearing", {"services": ["HearingAids"], "count": 1, "count_years": 3, "count_type": "Appliance"})])
    pe = est.estimate(data, 1, [Use("HearingAid", 2)], FEES)
    assert pe.total == 1000 and "limited to 1" in pe.lines[0].note
    # The same aid written as two lines (how "both ears" is often read) is still one a person...
    pe = est.estimate(data, 1, [Use("HearingAid", 1), Use("HearingAid", 1)], FEES)
    assert pe.total == 1000 and "limited to 1" in pe.lines[1].note
    # ...but two people each get their own.
    pe = est.estimate(data, 1, [Use("HearingAid", 1, people=2)], FEES)
    assert pe.total == 2000


def test_a_wait_is_reported_not_applied():
    data = _data(extras=[("Optical", True, 6)], benefits=[("OpticalSingleVisionLenses", 200, None)],
                 limits=[("optical", {"services": ["Optical"], "per_person": 250})])
    pe = est.estimate(data, 1, [Use("OpticalSingleVisionLenses", 1)], FEES)
    assert pe.total == 200 and "6 month wait" in pe.lines[0].note


def test_clean_uses_drops_unknown_items_and_clamps_counts():
    uses = est.clean_uses([{"item": "PhysiotherapySubsequent", "times": 500, "people": 0},
                           {"item": "NotAnItem", "times": 1}, "junk",
                           {"item": "OpticalSingleVisionLenses", "times": 0},
                           {"item": "HearingAid", "times": "2", "fee": "abc"}])
    assert [(u.item, u.times, u.people) for u in uses] == [("PhysiotherapySubsequent", est.MAX_TIMES, 1)]
    assert est.clean_uses("not a list") == []


def test_every_typical_fee_names_a_real_item_and_a_source():
    fees = est.typical_fees()
    from config.reference import EXTRAS_ITEMS
    assert set(fees) == set(EXTRAS_ITEMS)
    assert all(f["fee"] > 0 and f["source"] for f in fees.values())
