"""The everyday words the call screen understands in the browser (config/language.py, config/aliases.yaml).

Every word must map to a value the rest of the system uses, or the browser would build a request the
server drops without saying so.
"""
import re

from config import language as lang
from config.reference import FAMILY_TYPES, STATES


def test_every_word_maps_to_a_value_the_system_uses():
    assert lang.check() == []


def test_states_and_households_use_the_enumerations():
    assert set(lang.STATE_WORDS.values()) <= set(STATES) - {"ALL"}
    assert set(lang.HOUSEHOLD_WORDS.values()) <= set(FAMILY_TYPES)
    assert lang.HOUSEHOLD_WORDS["single mum"] == "single_parent"   # the longer phrase wins in the browser


def test_aliases_are_keyed_by_fund_code_and_lower_cased():
    aliases = lang.fund_aliases()
    assert all(re.fullmatch(r"[A-Z]{3}", code) for code in aliases)
    assert all(a == a.lower() and a.strip() for words in aliases.values() for a in words)
    assert "navy" in aliases["NHB"] and "our" in aliases["NHB"]
    assert "defence" in aliases["AHB"]


def test_navy_markers_are_never_dropped_as_filler_before_the_side_is_read():
    # "our" is a filler word for policy matching, but the browser reads it as a marker first.
    assert lang.NAVY_MARKERS <= lang.STOP_WORDS | lang.NAVY_MARKERS
    assert {"our", "navy"} <= lang.NAVY_MARKERS


def test_need_words_name_a_chip_or_a_benefit():
    for word, need in lang.NEED_WORDS.items():
        assert need["chip"] or need["features"], word


def test_benefit_words_for_the_browser_skip_filler():
    from app.call import _benefit_words
    found = _benefit_words()
    assert not set(found) & lang.STOP_WORDS
    assert all(len(w) >= 3 for w in found)
