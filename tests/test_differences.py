"""Which policy is better, benefit by benefit: the rules the card and the matrix follow.

Built from hand-made Values, so each rule is visible in the test: the direction, the materiality from
config/materiality.yaml, the excess as a trade-off, values that cannot be ranked, and chips moving a
benefit up the card.
"""
import pytest

from config.features import FEATURES
from engine import materiality as mat
from engine.benefits import UNLIMITED, Value
from engine.differences import COMP_BETTER, DIFFERENT, MISSING, NAVY_BETTER, SAME, TRADE_OFF, card, compare_pair, matrix


def cover(key, level):
    rank = {"NotCovered": 0, "Restricted": 1, "Covered": 2}[level]
    return Value(key, "cover_category", level, float(rank), float(rank))


def price(amount, excess=500):
    return Value("price", "premium", f"${amount} a month", -amount, amount, "dollars", f"at ${excess} excess")


def test_every_feature_key_is_a_real_feature():
    assert "cover:JointReplacements" in FEATURES and "item:DentalMajor615FullCrownVeneered" in FEATURES


def test_covered_against_not_covered_is_navy_better_and_decisive():
    d = compare_pair("cover:JointReplacements", cover("cover:JointReplacements", "Covered"),
                     cover("cover:JointReplacements", "NotCovered"))
    assert d.direction == NAVY_BETTER and d.materiality == 3


def test_restricted_against_covered_is_one_step_and_notable():
    d = compare_pair("cover:JointReplacements", cover("cover:JointReplacements", "Restricted"),
                     cover("cover:JointReplacements", "Covered"))
    assert d.direction == COMP_BETTER and d.materiality == 2


def test_a_cheaper_premium_is_better_and_banded_by_the_gap():
    assert compare_pair("price", price(300), price(340)).materiality == 3   # $40 gap
    assert compare_pair("price", price(300), price(315)).materiality == 2   # $15
    assert compare_pair("price", price(300), price(305)).materiality == 1   # $5
    d = compare_pair("price", price(300), price(301))                       # $1, under the $1.50 noise line
    assert d.direction == SAME and d.materiality == 0
    assert compare_pair("price", price(340), price(300)).direction == COMP_BETTER


def test_the_excess_is_a_trade_off_never_a_win():
    a = Value("excess", "excess", "$500 a person a year", None, 500.0)
    b = Value("excess", "excess", "$750 a person a year", None, 750.0)
    d = compare_pair("excess", a, b)
    assert d.direction == TRADE_OFF and d.materiality == 1


def test_limits_on_different_bases_are_not_ranked():
    per_person = Value("limit:DentalGeneral", "extras_limit", "$1,000 a person a year", 1000.0, 1000.0, "per_person")
    per_policy = Value("limit:DentalGeneral", "extras_limit", "$1,500 a policy a year", 1500.0, 1500.0, "per_policy")
    assert compare_pair("limit:DentalGeneral", per_person, per_policy).direction == DIFFERENT


def test_no_annual_limit_beats_any_dollar_limit():
    none = Value("limit:Optical", "extras_limit", "No annual limit", UNLIMITED, UNLIMITED, "any")
    some = Value("limit:Optical", "extras_limit", "$250 a person a year", 250.0, 250.0, "per_person")
    d = compare_pair("limit:Optical", none, some)
    assert d.direction == NAVY_BETTER and d.materiality == 2


def test_an_extras_benefit_gap_under_the_noise_line_is_the_same():
    a = Value("item:PhysiotherapyInitial", "extras_item", "$40", 40.0, 40.0, "dollars")
    b = Value("item:PhysiotherapyInitial", "extras_item", "$37", 37.0, 37.0, "dollars")
    assert compare_pair("item:PhysiotherapyInitial", a, b).direction == SAME


def test_a_wait_for_something_not_covered_is_left_to_the_cover_row():
    a = Value("wait:PregnancyBirth", "wait_months", "12 months", -12.0, 12.0, "months")
    b = Value("wait:PregnancyBirth", "wait_months", "Not covered", None, None)
    assert compare_pair("wait:PregnancyBirth", a, b).direction == MISSING


def test_a_shorter_wait_is_better():
    a = Value("wait:Other", "wait_months", "2 months", -2.0, 2.0, "months")
    b = Value("wait:Other", "wait_months", "12 months", -12.0, 12.0, "months")
    d = compare_pair("wait:Other", a, b)
    assert d.direction == NAVY_BETTER and d.materiality == 3


def _vals():
    navy = {
        "price": price(380), "cover:JointReplacements": cover("cover:JointReplacements", "Covered"),
        "cover:BackNeckSpine": cover("cover:BackNeckSpine", "Covered"),
        "cover:Cataracts": cover("cover:Cataracts", "Covered"),
        "cover:HeartVascular": cover("cover:HeartVascular", "Covered"),
    }
    comp = {
        "price": price(300, 750), "cover:JointReplacements": cover("cover:JointReplacements", "NotCovered"),
        "cover:BackNeckSpine": cover("cover:BackNeckSpine", "NotCovered"),
        "cover:Cataracts": cover("cover:Cataracts", "NotCovered"),
        "cover:HeartVascular": cover("cover:HeartVascular", "Covered"),
    }
    return {1: navy, 2: comp}


def test_the_card_splits_navy_strengths_from_what_to_watch():
    c = card(1, 2, _vals())
    assert {d.key for d in c.stronger} == {"cover:JointReplacements", "cover:BackNeckSpine", "cover:Cataracts"}
    assert [d.key for d in c.watch] == ["price"]
    assert [d.key for d in c.same] == ["cover:HeartVascular"]
    assert c.price.navy.note == "at $500 excess" and c.price.comp.note == "at $750 excess"


def test_a_chip_moves_its_benefits_to_the_top_of_equal_materiality():
    plain = card(1, 2, _vals())
    eyes = card(1, 2, _vals(), chips=["eyes"])
    assert plain.stronger[0].key != "cover:Cataracts"
    assert eyes.stronger[0].key == "cover:Cataracts" and eyes.stronger[0].chip_match


def test_a_card_can_be_limited_to_the_benefits_asked_about():
    c = card(1, 2, _vals(), keys=["price", "cover:Cataracts"])
    assert {d.key for d in c.stronger + c.watch + c.same} == {"price", "cover:Cataracts"}


def test_the_matrix_marks_the_best_value_and_fades_rows_that_agree():
    vals = _vals()
    vals[3] = {"price": price(350, 500), "cover:JointReplacements": cover("cover:JointReplacements", "Covered")}
    rows = {r.key: r for r in matrix([1, 2, 3], vals, show_all=True)}
    assert rows["price"].best == {1}            # $300 is the cheapest, in the second column
    assert rows["cover:JointReplacements"].best == {0, 2}
    assert rows["cover:HeartVascular"].best == set()   # the two that have it agree; the third does not list it


def test_the_matrix_never_marks_a_best_excess():
    vals = {1: {"excess": Value("excess", "excess", "$500", None, 500.0)},
            2: {"excess": Value("excess", "excess", "$750", None, 750.0)}}
    assert matrix([1, 2], vals, keys=["excess"])[0].best == set()


def test_the_default_matrix_keeps_core_rows_and_drops_other_rows_that_agree():
    rows = [r.key for r in matrix([1, 2], _vals())]
    assert "price" in rows and "cover:JointReplacements" in rows
    assert "cover:HeartVascular" not in rows


def test_materiality_file_loads_and_every_type_has_a_priority():
    cfg = mat.load()
    assert sorted(cfg["type_priority"]) == sorted(mat.TYPES)
    assert {c["key"] for c in mat.chips()} >= {"joints", "dental", "pregnancy"}


def test_an_item_takes_the_tags_of_its_service():
    assert "dental" in mat.tags_for("item:DentalMajor615FullCrownVeneered")
    assert "joints" in mat.tags_for("cover:JointReplacements")


def test_a_tag_naming_an_unknown_feature_fails_the_load(tmp_path):
    bad = mat.PATH.read_text(encoding="utf-8").replace("cover:HeartVascular]", "cover:Heart]")
    path = tmp_path / "materiality.yaml"
    path.write_text(bad, encoding="utf-8")
    mat.load.cache_clear()
    try:
        with pytest.raises(mat.MaterialityError, match="unknown features"):
            mat.load(str(path))
    finally:
        mat.load.cache_clear()
