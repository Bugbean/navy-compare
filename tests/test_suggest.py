"""What the call screen offers next (engine/suggest.py): the verdict line, the follow-up buttons and the
closest Navy Health policies. All deterministic, so no model and no database."""
from engine import suggest
from engine.benefits import Value
from engine.differences import card


def _vals(navy_price=380.0, comp_price=300.0, navy_excess=500, comp_excess=750):
    return {
        1: {"price": Value("price", "premium", f"${navy_price:.2f} a month", -navy_price, navy_price, "dollars",
                           f"at ${navy_excess} excess"),
            "cover:BackNeckSpine": Value("cover:BackNeckSpine", "cover_category", "Covered", 2.0, 2.0),
            "cover:HeartVascular": Value("cover:HeartVascular", "cover_category", "Covered", 2.0, 2.0)},
        2: {"price": Value("price", "premium", f"${comp_price:.2f} a month", -comp_price, comp_price, "dollars",
                           f"at ${comp_excess} excess"),
            "cover:BackNeckSpine": Value("cover:BackNeckSpine", "cover_category", "Not covered", 0.0, 0.0),
            "cover:HeartVascular": Value("cover:HeartVascular", "cover_category", "Covered", 2.0, 2.0)},
    }


def test_the_verdict_counts_wins_and_names_the_cheaper_side():
    line = suggest.verdict(card(1, 2, _vals()), "Core Silver+ Hospital", "HCF")
    assert line.startswith("Navy Health is better on 1 benefit and HCF on 0")
    assert "HCF is $80.00 a month cheaper (at a different excess)" in line


def test_the_verdict_says_nothing_about_excess_when_both_are_at_the_same_one():
    line = suggest.verdict(card(1, 2, _vals(navy_price=290.0, comp_excess=500)), "Core", "HCF")
    assert "Navy Health is $10.00 a month cheaper." in line and "different excess" not in line


def test_the_verdict_for_two_identical_policies():
    v = _vals(navy_price=300.0, comp_excess=500)
    v[2]["cover:BackNeckSpine"] = v[1]["cover:BackNeckSpine"]
    line = suggest.verdict(card(1, 2, v), "Core", "HCF")
    assert "the same premium" in line


def test_a_cheaper_navy_option_comes_first_when_theirs_is_cheaper():
    c = card(1, 2, _vals())
    ups = suggest.follow_ups(c, active_chips=set(), focus=[],
                             cheaper=[{"id": 9, "current_name": "Saver Bronze+ Hospital", "premium": 250.0}])
    assert ups[0]["kind"] == "swap_navy" and ups[0]["id"] == 9 and "$250.00 a month" in ups[0]["label"]
    assert ups[-1]["kind"] == "table" and len(ups) <= 5


def test_a_need_already_on_is_not_offered_again():
    c = card(1, 2, _vals())
    tags = {t for d in c.stronger + c.watch for t in d.tags}
    offered = {u.get("chip") for u in suggest.follow_ups(c, active_chips=tags, cheaper=[], focus=[])}
    assert not offered & tags


def test_clearing_the_focus_is_offered_only_when_there_is_one():
    c = card(1, 2, _vals())
    kinds = [u["kind"] for u in suggest.follow_ups(c, active_chips=set(), cheaper=[], focus=["price"])]
    assert "clear_focus" in kinds
    kinds = [u["kind"] for u in suggest.follow_ups(c, active_chips=set(), cheaper=[], focus=[])]
    assert "clear_focus" not in kinds


def test_the_closest_navy_policies_are_for_that_competitor_and_skip_ones_on_screen():
    closest = [{"comp_id": 2, "navy_id": 1, "navy_name": "Core", "score": 0.9},
               {"comp_id": 2, "navy_id": 3, "navy_name": "Saver", "score": 0.7},
               {"comp_id": 5, "navy_id": 4, "navy_name": "Other", "score": 0.95}]
    assert [m["id"] for m in suggest.closest_alternatives(closest, 2)] == [1, 3]
    assert [m["id"] for m in suggest.closest_alternatives(closest, 2, exclude={1})] == [3]
