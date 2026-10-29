"""engine/script.facts(): which of the card's differences reach the writer, and what each one tells it
about whether it matches what the member said they care about."""
from engine.benefits import Value
from engine.differences import Card, Difference
from engine.script import MAX_NEED_MATCHED_EXTRA, facts


def _d(key, direction="navy_better", materiality=2, chip_match=False):
    navy = Value(key, "cover_category", "Covered", 2.0, 2.0)
    comp = Value(key, "cover_category", "Not covered", 0.0, 0.0)
    return Difference(key=key, label=key, group="Clinical categories", kind="cover_category",
                      navy=navy, comp=comp, direction=direction, materiality=materiality, chip_match=chip_match)


def _price():
    navy = Value("price", "premium", "$300 a month", -300.0, 300.0, "dollars", "at $500 excess")
    comp = Value("price", "premium", "$320 a month", -320.0, 320.0, "dollars", "at $500 excess")
    return Difference(key="price", label="Premium a month", group="Price", kind="premium",
                      navy=navy, comp=comp, direction="navy_better", materiality=2)


def test_with_no_chips_active_it_is_exactly_top_five_strong_top_three_watch_and_excess():
    stronger = [_d(f"s{i}") for i in range(7)]
    watch = [_d(f"w{i}", "comp_better") for i in range(5)]
    card = Card(navy_id=1, comp_id=2, stronger=stronger, watch=watch, trade_offs=[], same=[], missing=[],
               price=_price())
    rows = facts(card, navy_name="Navy", comp_name="Comp", snapshot=1, state="NSW", family="couple")
    keys = [r["metric"] for r in rows]
    assert keys == ["price", "s0", "s1", "s2", "s3", "s4", "w0", "w1", "w2"]
    assert all("matches_what_member_cares_about" not in r["scope"] for r in rows)


def test_a_fact_matching_a_member_need_reaches_the_writer_even_below_the_cutoff():
    stronger = [_d(f"s{i}") for i in range(7)]            # s5, s6 fall outside the top-5 cutoff
    stronger[6].chip_match = True                          # ...but s6 matches what the member asked about
    card = Card(navy_id=1, comp_id=2, stronger=stronger, watch=[], trade_offs=[], same=[], missing=[], price=None)
    rows = facts(card, navy_name="Navy", comp_name="Comp", snapshot=1, state="NSW", family="couple")
    keys = [r["metric"] for r in rows]
    assert "s6" in keys and keys.index("s6") >= 5           # present, and not promoted ahead of the ranking
    matched = next(r for r in rows if r["metric"] == "s6")
    assert matched["scope"]["matches_what_member_cares_about"] is True


def test_a_need_matched_fact_already_in_the_top_cut_is_not_listed_twice():
    stronger = [_d(f"s{i}") for i in range(3)]
    stronger[1].chip_match = True
    card = Card(navy_id=1, comp_id=2, stronger=stronger, watch=[], trade_offs=[], same=[], missing=[], price=None)
    rows = facts(card, navy_name="Navy", comp_name="Comp", snapshot=1, state="NSW", family="couple")
    assert [r["metric"] for r in rows].count("s1") == 1


def test_a_service_wait_is_a_field_the_writer_can_bind_not_just_words_in_the_text():
    navy = Value("extras:Audiology", "extras_service", "Covered, 2 month wait", 1.0, None, wait=2.0)
    comp = Value("extras:Audiology", "extras_service", "Not covered", 0.0, None)
    d = Difference(key="extras:Audiology", label="Audiology", group="Extras services", kind="extras_service",
                   navy=navy, comp=comp, direction="navy_better", materiality=2)
    card = Card(navy_id=1, comp_id=2, stronger=[d], watch=[], trade_offs=[], same=[], missing=[], price=None)
    row = facts(card, navy_name="Navy", comp_name="Comp", snapshot=1, state="NSW", family="couple")[0]
    assert row["claimable"]["navy_wait_months"] == 2.0 and "comp_wait_months" not in row["claimable"]


def test_an_everyday_service_gap_reaches_the_writer_even_below_the_cutoff():
    stronger = [_d(f"s{i}") for i in range(7)]
    dental = _d("limit:DentalGeneral", materiality=1)
    rare = _d("extras:Orthoptics", materiality=1)
    card = Card(navy_id=1, comp_id=2, stronger=stronger + [rare, dental], watch=[], trade_offs=[], same=[],
                missing=[], price=None)
    keys = [r["metric"] for r in facts(card, navy_name="Navy", comp_name="Comp", snapshot=1, state="NSW",
                                       family="couple")]
    assert "limit:DentalGeneral" in keys and "extras:Orthoptics" not in keys


def test_need_matched_extras_are_capped():
    stronger = [_d(f"s{i}", chip_match=True) for i in range(MAX_NEED_MATCHED_EXTRA + 5)]
    card = Card(navy_id=1, comp_id=2, stronger=stronger, watch=[], trade_offs=[], same=[], missing=[], price=None)
    rows = facts(card, navy_name="Navy", comp_name="Comp", snapshot=1, state="NSW", family="couple")
    # The first 5 are the ordinary top-5 cut (all happen to be need-matched too); up to
    # MAX_NEED_MATCHED_EXTRA more of the rest get pulled in beyond that, not all of them.
    assert len(rows) == 5 + MAX_NEED_MATCHED_EXTRA
