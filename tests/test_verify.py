"""The binding verifier: what the rep reads aloud is what the facts say, side for side.

The case this design exists for is the first one: the competitor's figure stated as Navy Health's.
Both figures are in the same fact, so a check that only asks "is this number in a cited fact" passes it.
"""
from ai.facts import fields_of
from ai.verify import AnswerDraft, remap, render_facts, rescue, tokens_in, verify
from tests import ai_fixtures as fx


def draft(answer, *claims, unanswered=()):
    return AnswerDraft.model_validate({"answer_markdown": answer, "claims": list(claims),
                                       "unanswered": list(unanswered), "needs_clarification": False})


def claim(cid, text, facts, *bindings, kind="plain"):
    return {"claim_id": cid, "text": text, "kind": kind, "fact_ids": list(facts), "insight_ids": [],
            "bindings": list(bindings)}


CROWN = "Navy Health pays up to $1,000 on a crown, and Bupa Silver Plus pays up to $600."


def test_each_side_of_a_comparison_is_its_own_field():
    f = fields_of(fx.crown())
    assert f["value.navy"].value == 1000 and f["value.comp"].value == 600
    assert f["value.navy"].unit == "AUD"


def test_a_correctly_bound_answer_verifies():
    d = draft(CROWN, claim("c1", CROWN, ["F3"], "1,000|F3|value.navy", "600|F3|value.comp"))
    assert verify(d, fx.pack()).status == "VERIFIED"


def test_the_sides_swapped_is_caught():
    swapped = "Navy Health pays up to $600 on a crown, and Bupa Silver Plus pays up to $1,000."
    d = draft(swapped, claim("c1", swapped, ["F3"], "600|F3|value.navy", "1,000|F3|value.comp"))
    v = verify(d, fx.pack())
    assert v.status == "BLOCKED"
    assert any("value.navy is wrong" in f for f in v.failures)


def test_a_figure_no_fact_holds_is_caught():
    text = "Navy Health pays up to $1,200 on a crown."
    v = verify(draft(text, claim("c1", text, ["F3"])), fx.pack())
    assert v.status == "BLOCKED" and any("'1,200'" in f for f in v.failures)


def test_a_figure_a_cited_fact_holds_needs_no_explicit_binding():
    text = "Navy Health pays up to $1,000 on a crown."
    v = verify(draft(text, claim("c1", text, ["F3"])), fx.pack())
    assert v.status == "VERIFIED" and v.covered_by_evidence == 1


def test_a_percentage_must_bind_to_a_percentage():
    text = "Navy Health pays 1,000% more."
    d = draft(text, claim("c1", text, ["F3"], "1,000%|F3|value.navy"))
    assert any("percentage" in f for f in verify(d, fx.pack()).failures)


def test_record_language_on_incomplete_coverage_is_blocked_unless_hedged():
    text = "Navy Health has the shortest joint replacement wait at 12 months."
    v = verify(draft(text, claim("c1", text, ["F5"], "12|F5|value.navy")), fx.pack())
    assert v.status == "BLOCKED" and any("record language" in f for f in v.failures)
    hedged = "Navy Health has the shortest joint replacement wait among the policies held, at 12 months."
    v = verify(draft(hedged, claim("c1", hedged, ["F5"], "12|F5|value.navy")), fx.pack())
    assert v.status == "VERIFIED_WITH_SCOPE"


def test_a_known_name_no_cited_fact_is_about_is_caught():
    text = "Medibank Gold pays less on a crown."
    v = verify(draft(text, claim("c1", text, ["F3"])), fx.pack(), known_names=fx.known_names)
    assert any("'Medibank Gold' is named" in f for f in v.failures)


def test_names_the_cited_fact_carries_pass():
    v = verify(draft(CROWN, claim("c1", CROWN, ["F3"])), fx.pack(), known_names=fx.known_names)
    assert v.status == "VERIFIED"


def test_banned_words_are_blocked_whatever_the_facts_say():
    text = "Navy Health is the best: up to $1,000 on a crown."
    v = verify(draft(text, claim("c1", text, ["F3"])), fx.pack(), banned=["best", "guaranteed"])
    assert any("banned word 'best'" in f for f in v.failures)


def test_unknown_fact_ids_are_caught():
    v = verify(draft("Up to $1,000.", claim("c1", "Up to $1,000.", ["F99"])), fx.pack())
    assert any("unknown id" in f for f in v.failures)


def test_dates_are_one_token_not_three_numbers():
    toks = tokens_in("Data as of 1 Oct 2026, premium $412.35 a month")
    assert [t.kind for t in toks] == ["date", "number"]


def test_remap_fixes_a_pointer_with_exactly_one_home():
    text = "Bupa Silver Plus pays up to $600."
    d = draft(text, claim("c1", text, ["F3"], "600|F3|value.navy"))
    v = verify(d, fx.pack())
    fixed = remap(d, v, fx.pack())
    assert fixed is not None
    assert fixed[0].claims[0].bindings[0].field == "value.comp" and fixed[1].status == "VERIFIED"


def test_rescue_keeps_the_sentences_that_verify():
    good = "Navy Health pays up to $1,000 on a crown."
    bad = "Bupa Silver Plus pays up to $750."
    d = draft(f"{good} {bad}", claim("c1", good, ["F3"]), claim("c2", bad, ["F3"]))
    saved = rescue(d, verify(d, fx.pack()), fx.pack())
    assert saved is not None and saved[0].answer_markdown == good


def test_the_fallback_is_the_facts_own_words():
    out = render_facts(fx.pack(), prefer=["F3"])
    assert out.splitlines()[0] == "- Navy Health pays up to $1,000 on a crown; Bupa Silver Plus pays up to $600"
