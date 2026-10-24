"""The everyday words the call screen understands without a model: states, households and member needs.

The call screen reads what a rep types as they type it. Most of a request is one of a small set of
things (a fund, a policy, a state, who is covered, what the member cares about), and those are
recognised here deterministically, in the browser, in milliseconds. Only what is left over is sent to
the model. Every phrase maps to a value the rest of the system already uses, and tests/test_language.py
checks that they do.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from config.reference import FAMILY_TYPES, STATES

ALIASES_PATH = Path(__file__).resolve().parent / "aliases.yaml"

# A state, by code, full name or capital.
STATE_WORDS: dict[str, str] = {
    "nsw": "NSW", "new south wales": "NSW", "sydney": "NSW",
    "vic": "VIC", "victoria": "VIC", "melbourne": "VIC",
    "qld": "QLD", "queensland": "QLD", "brisbane": "QLD",
    "sa": "SA", "south australia": "SA", "adelaide": "SA",
    "wa": "WA", "western australia": "WA", "perth": "WA",
    "tas": "TAS", "tasmania": "TAS", "hobart": "TAS",
    "nt": "NT", "northern territory": "NT", "darwin": "NT",
    "act": "ACT", "canberra": "ACT",
}

# Who is covered. Longer phrases are tried first, so "single mum" is a single parent, not a single.
HOUSEHOLD_WORDS: dict[str, str] = {
    "single parent": "single_parent", "single mum": "single_parent", "single mother": "single_parent",
    "single dad": "single_parent", "single father": "single_parent", "sole parent": "single_parent",
    "just me": "single", "only me": "single", "myself": "single", "single": "single", "solo": "single",
    "couple": "couple", "partner": "couple", "husband": "couple", "wife": "couple", "spouse": "couple",
    "family": "family", "kids": "family", "children": "family", "family of": "family",
    "dependants only": "dependants_only", "child only": "dependants_only",
}

# What the member cares about: the chip it turns on (config/materiality.yaml) and the benefits it
# brings into the comparison.
NEED_WORDS: dict[str, dict] = {
    "knee": {"chip": "joints", "features": ["cover:JointReplacements", "cover:JointReconstructions"]},
    "knees": {"chip": "joints", "features": ["cover:JointReplacements", "cover:JointReconstructions"]},
    "hip": {"chip": "joints", "features": ["cover:JointReplacements"]},
    "hips": {"chip": "joints", "features": ["cover:JointReplacements"]},
    "joint": {"chip": "joints", "features": ["cover:JointReplacements", "cover:JointReconstructions"]},
    "joints": {"chip": "joints", "features": ["cover:JointReplacements", "cover:JointReconstructions"]},
    "back": {"chip": "joints", "features": ["cover:BackNeckSpine"]},
    "spine": {"chip": "joints", "features": ["cover:BackNeckSpine"]},
    "pregnant": {"chip": "pregnancy", "features": ["cover:PregnancyBirth", "wait:PregnancyBirth"]},
    "pregnancy": {"chip": "pregnancy", "features": ["cover:PregnancyBirth", "wait:PregnancyBirth"]},
    "baby": {"chip": "pregnancy", "features": ["cover:PregnancyBirth", "wait:PregnancyBirth"]},
    "ivf": {"chip": "pregnancy", "features": ["cover:AssistedReproductive"]},
    "teeth": {"chip": "dental", "features": ["extras:DentalGeneral", "extras:DentalMajor", "limit:DentalGeneral"]},
    "dental": {"chip": "dental", "features": ["extras:DentalGeneral", "extras:DentalMajor", "limit:DentalGeneral"]},
    "dentist": {"chip": "dental", "features": ["extras:DentalGeneral", "extras:DentalMajor", "limit:DentalGeneral"]},
    "braces": {"chip": "dental", "features": ["extras:Orthodontic", "limit:Orthodontic"]},
    "eyes": {"chip": "eyes", "features": ["cover:Cataracts", "cover:Eye", "extras:Optical"]},
    "cataract": {"chip": "eyes", "features": ["cover:Cataracts"]},
    "cataracts": {"chip": "eyes", "features": ["cover:Cataracts"]},
    "glasses": {"chip": "eyes", "features": ["extras:Optical", "limit:Optical"]},
    "heart": {"chip": "heart", "features": ["cover:HeartVascular"]},
    "cancer": {"chip": "cancer", "features": ["cover:ChemotherapyRadiotherapyImmunotherapy"]},
    "chemo": {"chip": "cancer", "features": ["cover:ChemotherapyRadiotherapyImmunotherapy"]},
    "mental": {"chip": "mental_health", "features": ["cover:HospitalPsychiatric", "extras:Psychology"]},
    "anxiety": {"chip": "mental_health", "features": ["cover:HospitalPsychiatric", "extras:Psychology"]},
    "depression": {"chip": "mental_health", "features": ["cover:HospitalPsychiatric", "extras:Psychology"]},
    "physio": {"chip": "physio", "features": ["extras:Physiotherapy", "limit:Physiotherapy"]},
    "chiro": {"chip": "physio", "features": ["extras:Chiropractic"]},
    "massage": {"chip": "physio", "features": ["extras:RemedialMassage"]},
    "retired": {"chip": "older", "features": []},
    "older": {"chip": "older", "features": []},
    "price": {"chip": None, "features": ["price"]},
    "cheaper": {"chip": None, "features": ["price"]},
    "cost": {"chip": None, "features": ["price"]},
    "premium": {"chip": None, "features": ["price"]},
    "excess": {"chip": None, "features": ["excess"]},
    "ambulance": {"chip": None, "features": ["ambulance"]},
    "wait": {"chip": None, "features": ["wait:PreExisting", "wait:Other"]},
    "waiting": {"chip": None, "features": ["wait:PreExisting", "wait:Other"]},
}

# Words that carry no meaning for the search and are dropped before policy matching.
STOP_WORDS = frozenset("""
a an the and or of for to with on in at by is are be it its this that these those my me i we us our ours
they them their his her he she you your vs versus against compare comparing comparison than about
what whats how hows which who policy policies cover covered covers plan plans insurance health fund
member members member's wants want looking moving move switching switch leaving thinking currently
on from like does do has have had any some please show tell can could would should get
add also instead again start new now then just one ones option options other another else
shes hes im ive theyre youre dont doesnt isnt worried concerned worry
""".split())

# Words that mark the policies around them as Navy Health's.
NAVY_MARKERS = frozenset({"our", "ours", "navy"})


def fund_aliases() -> dict[str, list[str]]:
    """config/aliases.yaml, lower-cased."""
    with open(ALIASES_PATH, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return {code: [str(a).lower() for a in words] for code, words in raw.items()}


def check() -> list[str]:
    """Problems with the vocabulary: a value the system does not use. Empty when all is well."""
    from config.features import FEATURES
    from engine.materiality import chips
    problems = [f"state word {w!r} -> {s}" for w, s in STATE_WORDS.items() if s not in STATES]
    problems += [f"household word {w!r} -> {f}" for w, f in HOUSEHOLD_WORDS.items() if f not in FAMILY_TYPES]
    chip_keys = {c["key"] for c in chips()}
    for w, need in NEED_WORDS.items():
        if need["chip"] is not None and need["chip"] not in chip_keys:
            problems.append(f"need word {w!r} -> unknown chip {need['chip']}")
        problems += [f"need word {w!r} -> unknown feature {k}" for k in need["features"] if k not in FEATURES]
    return problems
