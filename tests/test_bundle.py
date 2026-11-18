"""Merging a hospital policy plus any number of extras policies into one bundle side: the rules
`engine.bundle.merge_values` follows for each kind of Value it can see more than once, and the
`/card` route glue (`app.call._groups`/`_apply_groups`) that turns a rep-built group of real ids
into one synthetic column without touching the database.
"""
from app.call import _apply_groups, _groups
from engine.benefits import UNLIMITED, Value
from engine.bundle import merge_values


def service(covered, wait=None):
    return Value("extras:Dental", "extras_service", "x", 1.0 if covered else 0.0, None, wait=wait)


def limit(amount, basis="per_person"):
    return Value("limit:Dental", "extras_limit", "x", amount, amount, basis)


def item(amount, basis="dollars"):
    return Value("item:DentalCheckup", "extras_item", "x", amount, amount, basis)


def test_a_key_on_only_one_member_passes_through_unchanged():
    excess = Value("excess", "excess", "$500", -500.0, 500.0, "dollars")
    merged = merge_values([{"excess": excess}, {"extras:Dental": service(True)}])
    assert merged["excess"] is excess


def test_price_sums_every_members_premium():
    hospital = {"price": Value("price", "premium", "$300 a month", -300.0, 300.0, "dollars", "at $500 excess")}
    extras = {"price": Value("price", "premium", "$40 a month", -40.0, 40.0, "dollars", "no excess")}
    merged = merge_values([hospital, extras])
    assert merged["price"].size == 340.0 and merged["price"].display == "$340 a month"
    assert merged["price"].note == "at $500 excess"   # the hospital's note, not the extras' "no excess"


def test_price_is_omitted_if_any_member_has_no_rate_for_the_household():
    priced = {"price": Value("price", "premium", "$300 a month", -300.0, 300.0, "dollars")}
    unpriced: dict[str, Value] = {}
    merged = merge_values([priced, unpriced])
    assert "price" not in merged


def test_extras_service_is_covered_if_any_member_covers_it():
    merged = merge_values([{"extras:Dental": service(False)}, {"extras:Dental": service(True, wait=6)}])
    assert merged["extras:Dental"].rank == 1.0 and merged["extras:Dental"].wait == 6.0


def test_extras_service_wait_is_the_shorter_of_the_covering_members():
    merged = merge_values([{"extras:Dental": service(True, wait=6)}, {"extras:Dental": service(True, wait=2)}])
    assert merged["extras:Dental"].wait == 2.0


def test_extras_limit_dollar_amounts_on_the_same_basis_sum():
    merged = merge_values([{"limit:Dental": limit(500)}, {"limit:Dental": limit(300)}])
    assert merged["limit:Dental"].size == 800.0


def test_extras_limit_unlimited_beats_any_dollar_amount():
    unlimited = Value("limit:Dental", "extras_limit", "No annual limit", UNLIMITED, UNLIMITED, "any")
    merged = merge_values([{"limit:Dental": unlimited}, {"limit:Dental": limit(500)}])
    assert merged["limit:Dental"].size == UNLIMITED


def test_extras_limit_incompatible_bases_take_the_more_generous_not_a_wrong_sum():
    merged = merge_values([{"limit:Dental": limit(500, "per_person")}, {"limit:Dental": limit(1500, "per_policy")}])
    assert merged["limit:Dental"].size == 1500.0 and merged["limit:Dental"].basis == "per_policy"


def test_extras_item_dollar_amounts_sum():
    merged = merge_values([{"item:DentalCheckup": item(40)}, {"item:DentalCheckup": item(25)}])
    assert merged["item:DentalCheckup"].size == 65.0


def test_extras_item_percent_takes_the_higher_capped_at_100():
    merged = merge_values([{"item:DentalCheckup": item(60, "percent")}, {"item:DentalCheckup": item(80, "percent")}])
    assert merged["item:DentalCheckup"].size == 80.0


def test_a_three_way_bundle_merges_every_kind_at_once():
    hospital = {
        "excess": Value("excess", "excess", "$500", -500.0, 500.0, "dollars"),
        "price": Value("price", "premium", "$300 a month", -300.0, 300.0, "dollars", "at $500 excess"),
    }
    extras_a = {
        "price": Value("price", "premium", "$30 a month", -30.0, 30.0, "dollars", "no excess"),
        "extras:Dental": service(True, wait=2),
        "limit:Dental": limit(400),
    }
    extras_b = {
        "price": Value("price", "premium", "$25 a month", -25.0, 25.0, "dollars", "no excess"),
        "extras:Dental": service(True, wait=6),
        "limit:Dental": limit(300),
    }
    merged = merge_values([hospital, extras_a, extras_b])
    assert merged["excess"].size == 500.0           # hospital-only key, untouched
    assert merged["price"].size == 355.0             # 300 + 30 + 25
    assert merged["extras:Dental"].wait == 2.0        # the shorter of the two covering members
    assert merged["limit:Dental"].size == 700.0       # 400 + 300


def _policy(id_, name, product_type, is_navy):
    return {"id": id_, "name": name, "fund_name": "Navy Health" if is_navy else "nib", "fund_code": "NHB",
            "is_navy": is_navy, "product_type": product_type}


def test_groups_parses_plus_joined_ids_and_drops_unknown_or_singleton_ones():
    assert _groups({"groups": "1+2+3,4+5"}, [1, 2, 3, 4, 5]) == [[1, 2, 3], [4, 5]]
    assert _groups({"groups": "1+2,9"}, [1, 2]) == [[1, 2]]          # 9 was never in `ids`: dropped
    assert _groups({"groups": "1"}, [1]) == []                       # a group of one is not a bundle


def test_apply_groups_merges_a_hospital_and_an_extras_policy_into_one_synthetic_column():
    by_id = {101: _policy(101, "Core Silver+ Hospital", "hospital", True),
             201: _policy(201, "Budget Extras", "extras", True),
             501: _policy(501, "Their Combined", "combined", False)}
    vals = {101: {"price": Value("price", "premium", "$300 a month", -300.0, 300.0, "dollars"),
                  "excess": Value("excess", "excess", "$500", -500.0, 500.0, "dollars")},
            201: {"price": Value("price", "premium", "$40 a month", -40.0, 40.0, "dollars"),
                  "extras:Dental": service(True, wait=2)},
            501: {"price": Value("price", "premium", "$320 a month", -320.0, 320.0, "dollars")}}
    navy_ids, comp_ids = [101, 201], [501]
    group_meta = _apply_groups([[101, 201]], navy_ids, comp_ids, vals, by_id)

    assert len(navy_ids) == 1 and comp_ids == [501]
    bundle_id = navy_ids[0]
    assert bundle_id < 0   # synthetic: never a real database id
    assert group_meta == [[bundle_id, [101, 201]]]
    assert by_id[bundle_id]["name"] == "Core Silver+ Hospital + Budget Extras"
    assert by_id[bundle_id]["is_navy"] is True
    assert by_id[bundle_id]["product_type"] == "combined"   # mixes hospital + extras members
    assert vals[bundle_id]["price"].size == 340.0           # 300 + 40
    assert vals[bundle_id]["excess"].size == 500.0          # the hospital-only key, untouched


def test_apply_groups_leaves_a_group_alone_if_fewer_than_two_members_remain_on_one_side():
    by_id = {101: _policy(101, "Hospital", "hospital", True), 501: _policy(501, "Theirs", "combined", False)}
    vals = {101: {"price": Value("price", "premium", "$300 a month", -300.0, 300.0, "dollars")},
            501: {"price": Value("price", "premium", "$320 a month", -320.0, 320.0, "dollars")}}
    navy_ids, comp_ids = [101], [501]
    # 999 was named in the group but is not actually on the Navy side (or anywhere): dropped, leaving
    # only one real member, so there is nothing to merge and the side is untouched.
    group_meta = _apply_groups([[101, 999]], navy_ids, comp_ids, vals, by_id)
    assert navy_ids == [101] and group_meta == []
