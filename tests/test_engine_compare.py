"""The feature comparison table: sections, the navy column, and the agree/differ flags."""

from engine.compare import NOT_OFFERED, compare
from engine.db import Snapshot
from tests.test_engine_answer import needs_db


@needs_db
def test_the_table_has_hospital_extras_price_and_ambulance_for_a_hospital_pair():
    db = Snapshot()
    navy = db.rows("SELECT id, current_name, product_type FROM policies WHERE id = 2789")[0]
    comp = db.rows("SELECT id, current_name, product_type FROM policies WHERE id = 115")[0]
    table = compare(db, navy=navy, comp=comp, state="NSW", family="couple", scope="")
    titles = [s["title"] for s in table["sections"]]
    assert titles[0] == "Hospital" and "Clinical categories" in titles and "Ambulance" in titles
    cats = next(s for s in table["sections"] if s["title"] == "Clinical categories")
    assert len(cats["rows"]) == 38
    assert all(r["navy"] in ("covered", "restricted", "not covered", NOT_OFFERED) for r in cats["rows"])


@needs_db
def test_agree_flags_match_the_values():
    db = Snapshot()
    navy = db.rows("SELECT id, current_name, product_type FROM policies WHERE id = 2789")[0]
    comp = db.rows("SELECT id, current_name, product_type FROM policies WHERE id = 115")[0]
    table = compare(db, navy=navy, comp=comp, state="NSW", family="couple", scope="")
    for section in table["sections"]:
        for row in section["rows"]:
            assert row["same"] == (row["navy"] == row["comp"]), row


@needs_db
def test_premiums_at_different_excess_levels_are_not_paired():
    db = Snapshot()
    navy = db.rows("SELECT id, current_name, product_type FROM policies WHERE id = 2789")[0]
    comp = db.rows("SELECT id, current_name, product_type FROM policies WHERE id = 115")[0]
    table = compare(db, navy=navy, comp=comp, state="NSW", family="couple", scope="")
    price = next(s for s in table["sections"] if s["title"].startswith("Price"))
    labels = {r["feature"]: r for r in price["rows"]}
    assert labels["Premium a month, $500 excess"]["comp"] == "no rate at this excess"
    assert labels["Premium a month, $750 excess"]["navy"] == "no rate at this excess"
