"""The certification corpus is valid, and the harness scores it the way the plan says.

Coverage of every question type is checked here as a warning-free count, not a hard failure, until the
corpus is built in weeks 2 and 3; flip COVERAGE_REQUIRED when it is.
"""
import pytest

from tests.certification.harness import SOURCES, classify_item, classify_run, load_corpus, load_question_types, report
from tests.certification.modes import live_only

COVERAGE_REQUIRED = False
MIN_PER_TYPE = 5


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


@pytest.fixture(scope="module")
def types():
    return load_question_types()


def test_ids_are_unique(corpus):
    ids = [i["id"] for i in corpus]
    assert len(ids) == len(set(ids))


def test_every_item_is_well_formed(corpus, types):
    for item in corpus:
        assert item["question"].strip(), item["id"]
        assert item["source"] in SOURCES, item["id"]
        assert item["question_type"] in types, f"{item['id']}: unknown type {item['question_type']}"
        allowed = set(types[item["question_type"]]["slots"])
        assert set(item.get("slots") or {}) <= allowed, f"{item['id']}: slots outside {sorted(allowed)}"
        assert not set(item.get("must_cite") or []) & set(item.get("must_not_cite") or []), item["id"]


def test_every_question_type_has_items(corpus, types):
    counts = {t: sum(1 for i in corpus if i["question_type"] == t) for t in types}
    short = {t: n for t, n in counts.items() if n < MIN_PER_TYPE}
    if COVERAGE_REQUIRED:
        assert not short, f"types with fewer than {MIN_PER_TYPE} items: {short}"


ITEM = {"id": "X1", "question": "q", "question_type": "extras_item",
        "slots": {"policy": "Bupa Silver Plus", "service": "major_dental", "item": "615"},
        "must_cite": ["F3"], "must_not_cite": ["F9"], "plainness": {"max_grade": 8, "max_words": 20}}
GOOD = {"id": "X1", "question_type": "extras_item",
        "slots": {"policy": "bupa silver plus", "service": "major_dental", "item": "615"},
        "cited": ["F3"], "verifier_result": "pass", "text": "Navy Health pays up to $1,000 on a crown."}


def test_a_correct_run_passes():
    assert classify_run(ITEM, GOOD) == (True, [])


@pytest.mark.parametrize("change, why", [
    ({"question_type": "extras_limit"}, "routed to"),
    ({"cited": []}, "did not cite F3"),
    ({"cited": ["F3", "F9"]}, "make the answer wrong"),
    ({"verifier_result": "fallback"}, "verifier result"),
    ({"text": "Navy Health pays up to $1,000 on a crown, which is a great deal more than the other fund would ever consider paying."}, "words"),
])
def test_each_rule_fails_a_run(change, why):
    ok, notes = classify_run(ITEM, {**GOOD, **change})
    assert not ok and any(why in n for n in notes)


def test_an_item_passes_only_if_every_repeat_does():
    bad = {**GOOD, "cited": []}
    assert classify_item(ITEM, [GOOD, GOOD, GOOD])[0] == "PASS"
    assert classify_item(ITEM, [GOOD, GOOD, bad])[0] == "FLAKY"
    assert classify_item(ITEM, [bad, bad, bad])[0] == "FAIL"
    assert classify_item(ITEM, [])[0] == "NOT_RUN"


def test_the_report_counts_and_lists_failures():
    classified, md = report([ITEM], [GOOD, {**GOOD, "cited": []}, GOOD])
    assert classified[0]["category"] == "FLAKY"
    assert "## FLAKY" in md and "did not cite F3" in md


@live_only
def test_live_certification():
    pytest.skip("the live runner arrives with the router and writer (plan, week 3)")
