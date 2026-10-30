"""The call sheet (engine/claims.py): every extras service with its limit, what a visit pays and the gap
said in dollars, in the order members claim; the hospital conditions only one side covers; the price
gap a month and a year."""
from engine import claims as cl
from engine.benefits import Value
from engine.differences import compare_pair


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


def _service(svc, *, covered=True, limit=None, wait=None, items=()):
    """One policy's values for one extras service."""
    out = {f"extras:{svc}": Value(f"extras:{svc}", "extras_service", "Covered" if covered else "Not covered",
                                  1.0 if covered else 0.0, None, wait=wait)}
    if limit is not None:
        out[f"limit:{svc}"] = Value(f"limit:{svc}", "extras_limit", limit[0], limit[1], limit[1], limit[2])
    for item, display, amount in items:
        out[f"item:{item}"] = Value(f"item:{item}", "extras_item", display, amount, amount, "dollars")
    return out


def test_a_row_shows_each_side_limit_items_and_wait_and_says_the_gap_in_dollars():
    navy = _service("Physiotherapy", limit=("$800 a person a year", 800.0, "per_person"), wait=2.0,
                    items=[("PhysiotherapyInitial", "$60", 60.0), ("PhysiotherapySubsequent", "$50", 50.0)])
    comp = _service("Physiotherapy", limit=("$500 a person a year", 500.0, "per_person"),
                    items=[("PhysiotherapyInitial", "$60", 60.0), ("PhysiotherapySubsequent", "$50", 50.0)])
    rows, neither = cl.extras_sheet(1, 2, {1: navy, 2: comp})
    assert neither == [] and len(rows) == 1
    r = rows[0]
    assert r.label == "Physiotherapy" and r.direction == "navy_better"
    assert r.gap == "Navy Health: $300 more a year"
    assert r.navy.limit == "$800 a person a year" and r.navy.wait == "2 month wait"
    assert r.navy.items == [("first visit", "$60"), ("follow-up visit", "$50")]
    assert r.comp.wait == ""


def test_a_shared_limit_is_split_out_so_the_rep_can_say_what_it_is_shared_with():
    navy = _service("Orthotics", limit=("$300 a person a year (shared with Hearing aids, Glucose monitor)", 300.0, "per_person"))
    rows, _ = cl.extras_sheet(1, 2, {1: navy, 2: _service("Orthotics", covered=False)})
    assert rows[0].navy.limit == "$300 a person a year"
    assert rows[0].navy.shared == "Hearing aids, Glucose monitor"
    assert rows[0].comp.covered is False and rows[0].gap == "Navy Health only"


def test_with_equal_limits_the_gap_comes_from_what_each_visit_pays():
    rows, _ = cl.extras_sheet(1, 2, _vals())
    assert rows[0].direction == "comp_better"
    # The extraction (not covered vs $116) is the biggest item gap, and it is said in dollars.
    assert rows[0].gap == "Theirs: $116 more per tooth extraction"


def test_rows_follow_the_order_members_claim_with_what_the_rep_asked_about_first():
    navy = {**_service("Psychology"), **_service("Optical"), **_service("DentalGeneral")}
    comp = {**_service("Psychology"), **_service("Optical"), **_service("DentalGeneral")}
    rows, _ = cl.extras_sheet(1, 2, {1: navy, 2: comp})
    assert [r.service for r in rows] == ["DentalGeneral", "Optical", "Psychology"]
    rows, _ = cl.extras_sheet(1, 2, {1: navy, 2: comp}, focus=["Psychology"])
    assert [r.service for r in rows] == ["Psychology", "DentalGeneral", "Optical"]
    assert rows[0].focus and not rows[1].focus


def test_a_service_neither_covers_comes_last_so_everything_can_be_shown_side_by_side():
    navy = {**_service("HomeNursing", covered=False), **_service("Optical")}
    comp = {**_service("HomeNursing", covered=False), **_service("Optical")}
    rows, neither = cl.extras_sheet(1, 2, {1: navy, 2: comp})
    assert [r.service for r in rows] == ["Optical", "HomeNursing"] and neither == ["Home nursing"]
    assert rows[1].gap == "Neither covers it" and rows[1].direction == "same"


def _pair(key, kind, navy, comp, basis="dollars"):
    """A Difference between two values of one benefit, as the card scores it."""
    def v(x):
        display, size = x
        return Value(key, kind, display, size if kind != "premium" else -size, size, basis)
    return compare_pair(key, v(navy), v(comp))


def test_a_difference_too_small_to_score_is_said_as_minor_never_as_the_same():
    d = _pair("item:DentalGeneral012PeriodicExam", "extras_item", ("$47.50", 47.5), ("$45", 45.0))
    assert d.direction == "same"                     # below the card's noise threshold...
    assert cl.gap_words(d) == ("minor", "Minor: Navy Health pays $2.50 more")   # ...but still said


def test_differences_are_said_in_the_terms_of_the_benefit():
    assert cl.gap_words(_pair("limit:Optical", "extras_limit", ("$350", 350.0), ("$180", 180.0)))[1] \
        == "Navy Health: $170 more a year"
    assert cl.gap_words(_pair("price", "premium", ("$300 a month", 300.0), ("$340 a month", 340.0)))[1] \
        == "Navy Health: $40 a month cheaper"
    same = _pair("limit:Optical", "extras_limit", ("$350", 350.0), ("$350", 350.0))
    assert cl.gap_words(same) == ("same", "Same")


def test_a_service_covered_by_both_with_different_waits_says_whose_wait_is_shorter():
    navy = Value("extras:Optical", "extras_service", "Covered, 6 month wait", 1.0, None, wait=6.0)
    comp = Value("extras:Optical", "extras_service", "Covered, 2 month wait", 1.0, None, wait=2.0)
    assert cl.gap_words(compare_pair("extras:Optical", navy, comp)) == ("comp_better", "Theirs: 4 months shorter wait")


def test_hospital_rows_cover_every_category_with_the_same_ones_said_as_same():
    h = cl.hospital_sheet(1, 2, _hospital_vals(), costs=COSTS)
    assert [i.diff.key for i in h.differing] == ["cover:Psychiatric", "cover:BoneJointMuscle"]   # biggest bill first
    assert [(i.diff.key, i.gap) for i in h.same] == [("cover:HeartVascular", "Same: both cover it")]
    it = h.differing[1]
    assert (it.navy_status, it.comp_status, it.gap) == ("Covered", "Not covered", "Navy Health only")
    assert h.same[0].say == ""                       # nothing to sell where they are the same


def test_a_hospital_only_pair_has_no_extras_rows():
    assert cl.extras_sheet(1, 2, {1: {}, 2: {}}) == ([], [])


def test_the_service_order_lists_every_service_once():
    from config.reference import EXTRAS_SERVICES
    assert sorted(cl.SERVICE_ORDER) == sorted(EXTRAS_SERVICES)


def test_short_item_labels_drop_the_service_name_and_item_number():
    assert cl.short_item("PhysiotherapyInitial") == "first visit"
    assert cl.short_item("DentalMajor615FullCrownVeneered") == "crown"
    assert cl.short_item("Orthodontic881BracesUpperLowerPlusRetainer") == "braces, upper and lower, with retainer"


def test_categories_in_finds_cover_keys_and_ignores_everything_else():
    assert cl.categories_in(["cover:BoneJointMuscle", "price", "extras:Physiotherapy", "cover:HeartVascular"]) \
        == ["cover:BoneJointMuscle", "cover:HeartVascular"]
    assert cl.categories_in(["price", "wait:Other"]) == []


def _hospital_vals():
    def cover(key, display, rank):
        return Value(key, "cover_category", display, rank, rank)
    navy = {
        "cover:BoneJointMuscle": cover("cover:BoneJointMuscle", "Covered", 2.0),
        "cover:HeartVascular": cover("cover:HeartVascular", "Covered", 2.0),
        "cover:Psychiatric": cover("cover:Psychiatric", "Restricted", 1.0),
        "excess": Value("excess", "excess", "$500", 500.0, 500.0),
        "known_gap": Value("known_gap", "known_gap", "Yes", 1.0, None),
    }
    comp = {
        "cover:BoneJointMuscle": cover("cover:BoneJointMuscle", "Not covered", 0.0),
        "cover:HeartVascular": cover("cover:HeartVascular", "Covered", 2.0),
        "cover:Psychiatric": cover("cover:Psychiatric", "Covered", 2.0),
        "excess": Value("excess", "excess", "$750", 750.0, 750.0),
        "known_gap": Value("known_gap", "known_gap", "No", 0.0, None),
    }
    return {1: navy, 2: comp}


def test_the_hospital_sheet_lists_what_only_one_side_covers_and_counts_the_rest():
    h = cl.hospital_sheet(1, 2, _hospital_vals())
    assert [i.diff.key for i in h.navy_only] == ["cover:BoneJointMuscle"]
    assert [i.diff.key for i in h.comp_only] == ["cover:Psychiatric"]     # covered beats restricted
    assert h.both == 1
    keys = [d.key for d in h.essentials]
    assert keys == ["excess", "known_gap"]
    assert next(d for d in h.essentials if d.key == "excess").direction == "trade_off"


COSTS = {
    "cover:BoneJointMuscle": {"procedure": "a shoulder reconstruction", "private_cost": 18000, "public_wait_days": 240,
                              "matters_to": "active people with injuries", "source_cost": "MCF", "source_wait": "AIHW"},
    "cover:Psychiatric": {"procedure": "a mental health admission", "private_cost": 25000},
    "RESTRICTED": {"default_per_night": 400, "private_per_night": 1500},
}


def test_a_hospital_difference_says_what_the_member_pays_on_each_side_in_dollars_and_time():
    h = cl.hospital_sheet(1, 2, _hospital_vals(), costs=COSTS)
    it = h.navy_only[0]
    assert it.private_cost == 18000 and it.wait == "about 8 months" and it.procedure == "a shoulder reconstruction"
    assert it.navy_pays == "Covered: pays the $500 excess, plus any doctors' gap"
    assert it.comp_pays == ("Not covered: pays the whole private bill (typically $18,000), "
                            "or waits about 8 months as a public patient")
    assert it.say == ("If you ever need a shoulder reconstruction, which typically costs about $18,000 in a private "
                      "hospital, with Navy Health you'd pay the $500 excess plus any doctors' gap. Your current "
                      "policy doesn't cover it, so you'd pay it yourself or wait about 8 months in the public system.")


def test_restricted_cover_is_explained_in_money_not_left_as_a_word():
    h = cl.hospital_sheet(1, 2, _hospital_vals(), costs=COSTS)
    it = h.comp_only[0]                      # Navy restricted, theirs covered
    assert it.navy_pays.startswith("Restricted: paid only at the public-hospital rate (the government minimum, "
                                   "$400 a night, against about $1,500 a private hospital charges)")
    # Said to the member as honestly as a win would be: their policy is better here.
    assert it.say.startswith("Your current policy does cover a mental health admission")
    assert "this Navy Health policy only covers it at the public-hospital rate" in it.say
    # With no sourced private charge a night, only the minimum is stated - no comparison is made up.
    no_charge = {**COSTS, "RESTRICTED": {"default_per_night": 524}}
    it = cl.hospital_sheet(1, 2, _hospital_vals(), costs=no_charge).comp_only[0]
    assert "(the government minimum, $524 a night)" in it.navy_pays and "against" not in it.navy_pays


def test_without_a_sourced_figure_nothing_is_invented():
    h = cl.hospital_sheet(1, 2, _hospital_vals(), costs={})
    it = h.navy_only[0]
    assert it.private_cost is None and it.wait == ""
    assert "$" not in it.comp_pays.replace("$500", "") and "typically" not in it.say
    assert it.comp_pays == "Not covered: pays the whole private bill, or goes on the public waiting list"


def test_the_biggest_bill_comes_first_unless_the_rep_asked_about_one():
    vals = _hospital_vals()
    for key in ("cover:Cataracts", "cover:JointReplacements"):
        vals[1][key] = Value(key, "cover_category", "Covered", 2.0, 2.0)
        vals[2][key] = Value(key, "cover_category", "Not covered", 0.0, 0.0)
    costs = {**COSTS, "cover:Cataracts": {"private_cost": 3500}, "cover:JointReplacements": {"private_cost": 30000}}
    h = cl.hospital_sheet(1, 2, vals, costs=costs)
    assert [i.diff.key for i in h.navy_only] == ["cover:JointReplacements", "cover:BoneJointMuscle", "cover:Cataracts"]
    h = cl.hospital_sheet(1, 2, vals, costs=costs, focus={"cover:Cataracts"})
    assert h.navy_only[0].diff.key == "cover:Cataracts"


def test_every_clinical_category_has_cost_context_with_a_source_for_each_figure():
    from config.features import CATEGORY_LABELS
    costs = cl.category_costs()
    assert {k.removeprefix("cover:") for k in costs if k != "RESTRICTED"} == set(CATEGORY_LABELS)
    for key, c in costs.items():
        if key == "RESTRICTED":
            assert c["default_per_night"] and c["source"]
            continue
        assert c["procedure"] and c["matters_to"], key
        assert "(" not in (c.get("cost_words") or ""), f"{key}: cost_words are shown inside brackets"
        if c.get("private_cost"):
            assert c["source_cost"] and c["url_cost"], key
        if c.get("public_wait_days"):
            assert c["source_wait"] and c["url_wait"], key


def test_an_extras_only_pair_has_no_hospital_sheet():
    assert cl.hospital_sheet(1, 2, {1: _service("Optical"), 2: _service("Optical")}) is None


def _price(navy, comp):
    n = Value("price", "premium", f"${navy} a month", -navy, navy, "dollars")
    c = Value("price", "premium", f"${comp} a month", -comp, comp, "dollars")
    return compare_pair("price", n, c)


def test_the_price_gap_is_given_a_month_and_a_year():
    g = cl.price_gap(_price(743.86, 843.60))
    assert g.cheaper == "navy_better" and g.month == 99.74 and g.year == 1196.88
    assert cl.price_gap(_price(300.0, 250.0)).cheaper == "comp_better"
    assert cl.price_gap(_price(300.0, 300.0)).cheaper == "same"
    assert cl.price_gap(None) is None


