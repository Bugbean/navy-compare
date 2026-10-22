"""The claim-level verifier: each sentence's figures and names are in the facts it cites."""
from ai.facts import AnswerWriterOutput, Claim, FactBundle
from ai.verify_claims import spell_out_numbers, verify_answer
from tests import ai_fixtures as fx


def bundle():
    return FactBundle(bundle_id="b1", facts=[fx.premium(), fx.crown(), fx.wait()], generated_at="2026-10-23")


def out(answer, *claims):
    return AnswerWriterOutput(answer=answer, claims=[Claim(claim_id=c, text=t, fact_ids=f) for c, t, f in claims])


GOOD = "Navy Health pays up to $1,000 on a crown, and Bupa Silver Plus pays up to $600."


def test_a_supported_sentence_verifies():
    assert verify_answer(out(GOOD, ("c1", GOOD, ["F3"])), bundle()).status == "VERIFIED"


def test_an_invented_figure_is_blocked():
    text = "Navy Health pays up to $1,500 on a crown."
    r = verify_answer(out(text, ("c1", text, ["F3"])), bundle())
    assert r.status == "BLOCKED" and "1,500" in r.unverified_claims[0]


def test_numbers_written_as_words_are_read_as_numbers():
    assert spell_out_numbers("one thousand dollars, twice a year") == "1000 dollars, 2 a year"
    text = "Navy Health pays up to fifteen hundred dollars."
    assert verify_answer(out(text, ("c1", text, ["F3"])), bundle()).status == "BLOCKED"


def test_a_name_the_cited_facts_do_not_hold_is_blocked():
    text = "Medibank Gold pays up to $600 on a crown."
    r = verify_answer(out(text, ("c1", text, ["F3"])), bundle())
    assert "Medibank Gold" in r.unverified_claims[0]


def test_absolute_language_needs_complete_coverage():
    text = "Navy Health has the shortest wait, 12 months."
    r = verify_answer(out(text, ("c1", text, ["F5"])), bundle())
    assert r.status == "BLOCKED" and "absolute claim" in r.unverified_claims[0]


def test_a_figure_outside_every_claim_is_blocked():
    text = GOOD + " That saves you $90."
    r = verify_answer(out(text, ("c1", GOOD, ["F3"])), bundle())
    assert r.status == "BLOCKED" and r.unverified_claims[0].startswith("answer:")


def test_one_bad_claim_is_dropped_and_the_rest_kept():
    bad = "Bupa Silver Plus pays up to $750."
    r = verify_answer(out(f"{GOOD} {bad}", ("c1", GOOD, ["F3"]), ("c2", bad, ["F3"])), bundle())
    assert r.status == "VERIFIED_PARTIAL" and r.partial_answer == GOOD


def test_banned_words_block():
    text = "Navy Health is guaranteed to pay up to $1,000 on a crown."
    r = verify_answer(out(text, ("c1", text, ["F3"])), bundle(), banned=["guaranteed"])
    assert r.status == "BLOCKED"
