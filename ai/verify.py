"""The binding verifier: every figure in the answer is bound to a field of a Fact that holds it.

A regex scan for numbers anywhere in the cited facts passes a wrong-column number: on a card, Navy
Health's $1,000 crown limit stated as the competitor's. Here the model says, for every number and date
it writes, which fact and which field it came from (a Binding); this module checks each binding against
the EvidencePack and that nothing numeric in the prose is left unbound.

Checks, in order:
  1. the answer is not empty
  2. every claim cites facts (or insights) the pack holds; every binding's fact is one the claim cites
  3. every binding is true: the field exists, its kind matches the token, the value matches at the
     token's own precision, a "%" token binds a percentage
  4. every numeric / date token in the prose has a true binding with the same value
     (markdown structure - list markers, table rules, headings - is not a token)
  5. a claim tagged superlative cites only complete facts; an untagged claim with record language is
     sent back to be tagged or reworded
  6. every known policy or fund name in the prose is a subject of a cited fact, or named by one
  7. every default the code applied is stated in the answer
  8. no banned word ("guaranteed", "cheapest", medical advice verbs) appears anywhere
The result lists each failure in words the model can act on; ai/compose.py feeds them back once.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .facts import EvidencePack, FieldValue, as_number, date_iso, pair_parts, range_start, time_hhmm

# --- the draft the model returns -------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Binding(_Strict):
    token: str
    fact_id: str
    field: str

    @model_validator(mode="before")
    @classmethod
    def _from_compact(cls, value: Any) -> Any:
        """A binding arrives from the model as one string, "token|fact_id|field" - roughly half the
        output tokens of the object form, on the part of a draft that carries the most of them. The
        object form is still accepted."""
        if isinstance(value, str):
            parts = value.split("|")
            if len(parts) != 3 or not all(p.strip() for p in parts):
                raise ValueError(f"a binding is 'token|fact_id|field', not {value!r}")
            token, fact_id, field = (p.strip() for p in parts)
            return {"token": token, "fact_id": fact_id, "field": field}
        return value


class ClaimDraft(_Strict):
    claim_id: str
    text: str
    kind: Literal["plain", "superlative"] = "plain"
    fact_ids: list[str] = Field(default_factory=list)
    insight_ids: list[str] = Field(default_factory=list)
    bindings: list[Binding] = Field(default_factory=list)


class AnswerDraft(_Strict):
    answer_markdown: str
    claims: list[ClaimDraft] = Field(default_factory=list)
    unanswered: list[str] = Field(default_factory=list)
    needs_clarification: bool = False


ANSWER_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "answer_markdown": {"type": "string", "description": "What the rep reads to the member: plain sentences."},
        "claims": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "claim_id": {"type": "string"},
                "text": {"type": "string", "description": "One sentence of the answer that states facts."},
                "kind": {"type": "string", "enum": ["plain", "superlative"],
                         "description": "superlative when the sentence claims a most/fewest, a never/only."},
                "fact_ids": {"type": "array", "items": {"type": "string"}},
                "insight_ids": {"type": "array", "items": {"type": "string"}},
                # One string per figure, "token|fact_id|field": the token exactly as written in the answer,
                # the fact it came from, the field of that fact holding it.
                "bindings": {"type": "array", "items": {
                    "type": "string",
                    "description": "token|fact_id|field - e.g. \"1,000|F3|value.navy\" or \"600|F3|value.comp\"."}}},
            "required": ["claim_id", "text", "kind", "fact_ids", "insight_ids", "bindings"],
            "additionalProperties": False}},
        "unanswered": {"type": "array", "items": {"type": "string"},
                       "description": "Parts of the question the facts could not answer, in the rep's words."},
        "needs_clarification": {"type": "boolean",
                                "description": "true when the answer is a question back to the rep (an ambiguous policy)."},
    },
    "required": ["answer_markdown", "claims", "unanswered", "needs_clarification"],
    "additionalProperties": False,
}


# --- tokens in prose -------------------------------------------------------------------------------

_YEAR_RANGE_TOKEN = re.compile(r"(?<![\w.])(\d{4})\s*[-/–]\s*(\d{2}|\d{4})(?![\w])")
_ISO_DATE = re.compile(r"(?<![\w.])(\d{4})-(\d{2})-(\d{2})(?![\w])")
_MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
# "1 Oct 2026" and "1 October 2026" are the same date; read either as one date token, not as the numbers
# 1 and 2026 the prose then cannot bind.
_MONTH_FORMS = _MONTHS + "|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
_WORD_DATE = re.compile(r"(?<![\w.])(\d{1,2})\s+(" + _MONTH_FORMS + r")\.?\s+(\d{4})(?![\w])", re.I)
_CLOCK = re.compile(r"(?<![\w.:])(\d{1,2}):(\d{2})(?:\s*(am|pm))?(?![\w:])", re.I)
_NUMBER = re.compile(r"(?<![\w.])([+-]?)(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(\s*%)?(?:st|nd|rd|th)?(?![\w])")
_LIST_MARKER = re.compile(r"^\s*(?:\d{1,2}[.)]|[-*+])\s+", re.M)
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$", re.M)
_HEADING = re.compile(r"^#{1,6}\s+", re.M)
_COUNT_WORDS = re.compile(r"\b(once|twice)\b", re.I)
# A count spelled as a word is a figure ("three visits"). "One" is left alone: "one of the policies" is
# not a count, and the prompt forbids spelling a count as a word anyway.
_NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                 "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
                 "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20}
_NUMBER_WORD = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", re.I)
# An ordinal spelled as a word is a figure too (a rank), except a time ("third time"). "First" and
# "second" are left alone: "first visit", "second opinion".
_ORDINAL_WORDS = {"third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9,
                  "tenth": 10, "eleventh": 11, "twelfth": 12}
_ORDINAL_WORD = re.compile(r"\b(" + "|".join(_ORDINAL_WORDS) + r")\b(?!-|\s+(?:time|year|month|week|day))", re.I)
_RECORD_LANGUAGE = re.compile(
    r"\b(never|ever|all[- ]time|of all time|in history|on record|record|most|fewest|least|"
    r"highest|lowest|biggest|largest|longest|shortest|best|worst)\b", re.I)
# A record word that is honest about incomplete data: "the lowest among the policies held", "the most
# since 2024". Such a claim is accepted with a caveat instead of being blocked; the reader has been told.
_HEDGED_RECORD = re.compile(
    r"\b(?:among|in|of|across|within|from) the (?:\w+ )?(?:policies|products|funds|data|rows|records?) "
    r"(?:held|recorded|available|on file|with [\w -]+ data|that (?:hold|have|carry|record)s? [\w -]+)\b|"
    r"\b(?:since|from) (?:the )?\d{4}(?:[-/]\d{2,4})?\b|"
    r"\bwhere (?:the )?(?:\w+ )?(?:figure|data)s? (?:is|are|was|were) (?:held|recorded)\b", re.I)
# A digit that is a rate basis is not a figure: "per 100 members". ("$100" stays a figure.)
_RATE_BASIS_BEFORE = re.compile(r"\bper[\s-]*$", re.I)
_MONTH_NUMBER = {m: i + 1 for i, m in enumerate(_MONTHS.split("|"))}
_MONTH_NUMBER.update({m[:3]: n for m, n in list(_MONTH_NUMBER.items())})
_MONTH_NUMBER["sept"] = 9


def _clock_value(hour: str, minute: str, meridiem: Optional[str]) -> str:
    h = int(hour)
    if meridiem:
        if meridiem.lower() == "pm" and h < 12:
            h += 12
        elif meridiem.lower() == "am" and h == 12:
            h = 0
    return f"{h}:{minute}"


@dataclass(frozen=True)
class Token:
    text: str
    kind: str                  # number | year_range | date | time
    value: Any                 # float, start year, or ISO date
    places: int = 0
    percent: bool = False
    start: int = 0
    signed: bool = False       # the prose wrote an explicit "+" or "-" in front of the number

    def _replace_value(self, value: Any) -> "Token":
        return Token(self.text, self.kind, value, self.places, self.percent, self.start, self.signed)


def _strip_structure(text: str) -> str:
    text = _TABLE_RULE.sub(lambda m: " " * len(m.group(0)), text)
    text = _HEADING.sub(lambda m: " " * len(m.group(0)), text)
    return _LIST_MARKER.sub(lambda m: " " * len(m.group(0)), text)


def tokens_in(text: str) -> list[Token]:
    """Every figure a reader would take as a fact, with its position. Dates and year ranges are read
    first and blanked so their digits are not also read as numbers."""
    working = _strip_structure(text)
    found: list[Token] = []

    def blank(m: re.Match) -> str:
        return " " * len(m.group(0))

    # ISO dates before ranges: "2026-10-01" would otherwise be read as the range "2026-10" and -1.
    for m in _ISO_DATE.finditer(working):
        found.append(Token(m.group(0), "date", f"{m.group(1)}-{m.group(2)}-{m.group(3)}", start=m.start()))
    working = _ISO_DATE.sub(blank, working)
    for m in _YEAR_RANGE_TOKEN.finditer(working):
        found.append(Token(m.group(0), "year_range", int(m.group(1)), start=m.start()))
    working = _YEAR_RANGE_TOKEN.sub(blank, working)
    for m in _WORD_DATE.finditer(working):
        iso = f"{m.group(3)}-{_MONTH_NUMBER[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
        found.append(Token(m.group(0), "date", iso, start=m.start()))
    working = _WORD_DATE.sub(blank, working)
    for m in _CLOCK.finditer(working):
        found.append(Token(m.group(0), "time", _clock_value(m.group(1), m.group(2), m.group(3)), start=m.start()))
    working = _CLOCK.sub(blank, working)
    for m in _NUMBER.finditer(working):
        if _RATE_BASIS_BEFORE.search(working[:m.start()]):
            continue
        whole, frac = m.group(2).replace(",", ""), m.group(3) or ""
        value = float(whole + frac)
        if m.group(1) == "-":
            value = -value
        found.append(Token(m.group(0).strip(), "number", value, places=len(frac) - 1 if frac else 0,
                           percent=bool(m.group(4)), start=m.start(), signed=bool(m.group(1))))
    for m in _COUNT_WORDS.finditer(working):
        found.append(Token(m.group(0), "number", 1.0 if m.group(0).lower() == "once" else 2.0, start=m.start()))
    for m in _NUMBER_WORD.finditer(working):
        found.append(Token(m.group(0), "number", float(_NUMBER_WORDS[m.group(1).lower()]), start=m.start()))
    for m in _ORDINAL_WORD.finditer(working):
        found.append(Token(m.group(0), "number", float(_ORDINAL_WORDS[m.group(1).lower()]), start=m.start()))
    return sorted(found, key=lambda t: t.start)


def _binding_problem(text: str) -> str:
    found = tokens_in(text)
    if len(found) > 1:
        return (f"binding token {text!r} holds {len(found)} figures ({', '.join(t.text for t in found)}): bind each "
                f"one separately to its own field")
    return f"binding token {text!r} is not a number or date"


# --- checking a binding against a field -------------------------------------------------------------

def _same(held: float, token: Token) -> bool:
    """The field's number is the token at the token's own precision. Half-way values round either way;
    a difference beyond half a unit of the last place written is a different number."""
    return abs(held - token.value) <= 0.5 * 10 ** (-token.places) + 1e-9


def _holds(fv: FieldValue, token: Token) -> Optional[str]:
    """None when the field holds the token; otherwise why not."""
    if token.kind == "year_range":
        start = range_start(fv.value)
        if fv.kind != "year_range" or start != token.value:
            return f"field holds {fv.value!r}, not {token.text}"
        return None
    if token.kind == "date":
        if fv.kind != "date" or date_iso(fv.value) != token.value:
            return f"field holds {fv.value!r}, not date {token.text}"
        return None
    if token.kind == "time":
        if fv.kind != "time" or time_hhmm(fv.value) != token.value:
            return f"field holds {fv.value!r}, not time {token.text}"
        return None
    if fv.kind == "pair":
        parts = pair_parts(fv.value) or ()
        if any(_same(p, token) for p in parts):
            return None
        return f"field holds {fv.value!r}, not {token.text}"
    if fv.kind != "number":
        return f"field holds {fv.value!r}, which is not a number"
    held = as_number(fv.value)
    if held is None:
        return f"field holds {fv.value!r}, which is not a number"
    if not _same(held, token) and not _same(abs(held), token._replace_value(abs(token.value))):
        # A difference's sign is the order it was taken in ("navy minus competitor"), which a reader is
        # not told; the prose carries the direction in words ("$12 a month less"). The magnitude has to match.
        return f"field holds {fv.value}, not {token.text}"
    if token.percent and not _is_percent(fv.unit):
        return f"the token is a percentage but the field's unit is {fv.unit or 'unknown'}"
    return None


def _is_percent(unit: Optional[str]) -> bool:
    return bool(unit) and ("percent" in unit or "pct" in unit or unit == "%")


def _find_holder(pack: EvidencePack, tok: Token, prefer: list[str], *, only_prefer: bool = False) -> Optional[str]:
    """Where this token's value actually lives in the pack, if anywhere: `fact_id.field`. The claim's own
    cited facts are searched first, so a wrong-field binding is named before a same-value field on an
    unrelated fact."""
    for fact_id in dict.fromkeys([*prefer] if only_prefer else [*prefer, *pack.facts]):
        for name, fv in pack.fields(fact_id).items():
            if _holds(fv, tok) is None:
                return f"{fact_id}.{name}"
    return None


def _find_holders(pack: EvidencePack, tok: Token, prefer: list[str]) -> list[tuple[str, str]]:
    """Every (fact_id, field) in the whole pack that holds this token's value, as pairs (never joined:
    a semantic key can itself contain dots). remap() acts only when there is exactly one."""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for fact_id in dict.fromkeys([*prefer, *pack.facts]):
        for name, fv in pack.fields(fact_id).items():
            if _holds(fv, tok) is None:
                pair = (fact_id, name)
                if pair not in seen:
                    seen.add(pair)
                    out.append(pair)
    return out


# --- the verdict -----------------------------------------------------------------------------------

@dataclass
class RemapCandidate:
    """One failure remap() may fix with no model call: a wrong binding ("binding") or a prose token with
    no binding at all ("unbound"), whose value lives in exactly one place in the pack."""
    kind: Literal["binding", "unbound"]
    claim_id: str
    token: Token
    binding_index: Optional[int]
    holders: list[tuple[str, str]]


@dataclass
class Verdict:
    status: Literal["VERIFIED", "VERIFIED_WITH_SCOPE", "BLOCKED"]
    failures: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    tokens: int = 0
    bound: int = 0
    covered_by_evidence: int = 0    # numbers accepted because a cited fact holds them, with no binding written
    remap_candidates: list[RemapCandidate] = field(default_factory=list)


NameIndex = Callable[[], dict[str, str]]       # casefolded full name -> canonical id
AliasIndex = Callable[[], dict[str, set[str]]]  # canonical id -> every casefolded name it goes by


def _banned_hits(text: str, banned: Iterable[str]) -> list[str]:
    hits = []
    for word in banned:
        w = word.strip()
        if w and re.search(r"(?<![\w'])" + re.escape(w) + r"(?![\w'])", text, re.I):
            hits.append(w)
    return hits


def verify(draft: AnswerDraft, pack: EvidencePack, *, known_names: Optional[NameIndex] = None,
           aliases: Optional[AliasIndex] = None, question: str = "",
           banned: Iterable[str] = ()) -> Verdict:
    failures: list[str] = []
    caveats: list[str] = []
    remap_candidates: list[RemapCandidate] = []
    answer = draft.answer_markdown or ""
    if not answer.strip():
        return Verdict("BLOCKED", ["the answer is empty"])
    claims = list(draft.claims)
    prose = answer

    # A binding may name a fact by its semantic key: resolve every one to the real id first.
    for c in claims:
        c.fact_ids = [pack.resolve_id(f) for f in c.fact_ids]
        for b in c.bindings:
            b.fact_id = pack.resolve_id(b.fact_id)

    # 2. citations
    cited: set[str] = set()
    for c in claims:
        unknown = [f for f in c.fact_ids if f not in pack.facts] + [i for i in c.insight_ids if i not in pack.insights]
        if unknown:
            failures.append(f"claim {c.claim_id} cites unknown id(s): {', '.join(unknown)}")
        cited.update(f for f in c.fact_ids if f in pack.facts)
        for i in c.insight_ids:
            ins = pack.insights.get(i)
            if ins is not None:
                cited.update(ins.supporting_fact_ids)
        for b in c.bindings:
            if b.fact_id not in c.fact_ids and b.fact_id in pack.facts:
                c.fact_ids.append(b.fact_id)         # a binding is a citation; accept it as one
                cited.add(b.fact_id)

    # 3. bindings
    true_values: dict[tuple, int] = {}
    for c in claims:
        for bi, b in enumerate(c.bindings):
            figures = tokens_in(b.token)
            if not figures:
                continue                             # a binding with no figure says nothing to mislead with
            fields = pack.fields(b.fact_id)
            if not fields:
                failures.append(f"claim {c.claim_id}: fact {b.fact_id} is not in the evidence")
                continue
            # "fields.value.navy" for "value.navy": the model copying the heading the fact was shown under.
            fv = fields.get(b.field) or fields.get(b.field.removeprefix("fields."))
            if fv is None:
                failures.append(f"claim {c.claim_id}: fact {b.fact_id} has no field {b.field!r} "
                                f"(fields: {', '.join(list(fields)[:12])})")
                continue
            if len(figures) > 1:
                held_any = False
                for sub in figures:
                    if _holds(fv, sub) is None:
                        true_values[_key(sub)] = true_values.get(_key(sub), 0) + 1
                        held_any = True
                if not held_any:
                    failures.append(f"claim {c.claim_id}: {_binding_problem(b.token)} (none of them is at "
                                    f"{b.fact_id}.{b.field}, which holds {fv.value!r})")
                continue
            tok = figures[0]
            why = _holds(fv, tok)
            if why:
                holders = _find_holders(pack, tok, c.fact_ids)
                msg = f"claim {c.claim_id}: {b.token!r} bound to {b.fact_id}.{b.field} is wrong: {why}"
                if holders:
                    msg += f" (the value is at {holders[0][0]}.{holders[0][1]})"
                failures.append(msg)
                if len(holders) == 1:
                    remap_candidates.append(RemapCandidate("binding", c.claim_id, tok, bi, holders))
                continue
            true_values[_key(tok)] = true_values.get(_key(tok), 0) + 1

    # 4. every token in the prose is bound. A date a cited fact holds is accepted without an explicit
    # binding: a label has no wrong column to be read from. A number is accepted without a binding only
    # when a fact the draft cites holds exactly that value.
    held_labels = _held_labels(pack, cited)
    held_in_notes = _figures_in_notes(pack, cited)
    found = tokens_in(prose)
    unbound: list[Token] = []
    covered_by_evidence = 0
    for tok in found:
        if _key(tok) in true_values:
            continue
        if tok.kind == "number" and tok.percent and (tok.kind, round(tok.value, tok.places), tok.places, False) in true_values:
            continue
        if tok.kind in ("year_range", "date", "time") and (tok.kind, tok.value) in held_labels:
            continue
        if tok.kind == "number" and (_find_holder(pack, tok, list(cited), only_prefer=True)
                                     or any(_same(v, tok) for v in held_in_notes)):
            covered_by_evidence += 1
            continue
        unbound.append(tok)
    seen_text: set[str] = set()
    for tok in unbound:
        if tok.text in seen_text:
            continue
        seen_text.add(tok.text)
        holders = _find_holders(pack, tok, list(cited))
        msg = (f"'{tok.text}' appears in the answer but no true binding covers it: bind it to the fact "
               f"field that holds it, or remove it")
        if holders:
            msg += f" (try {holders[0][0]}.{holders[0][1]})"
        failures.append(msg)
        if len(holders) == 1:
            owner = next((c.claim_id for c in claims if tok.text in c.text), None)
            if owner is not None:
                remap_candidates.append(RemapCandidate("unbound", owner, tok, None, holders))

    # 5. record language rests on complete facts, tagged or not (the tag is not the safety, the coverage is)
    for c in claims:
        facts = [f for f in c.fact_ids if f in pack.facts]
        m = _RECORD_LANGUAGE.search(c.text)
        if c.kind != "superlative" and m is None:
            continue
        incomplete = [f for f in facts if not pack.complete(f)]
        if incomplete:
            word = m.group(0) if m else "a superlative"
            notes = {pack.facts[f].coverage.reason for f in incomplete if pack.facts[f].coverage.reason}
            if m is not None and _HEDGED_RECORD.search(c.text):
                caveats.append(f"'{word}' in \"{c.text.strip()[:80]}\" rests on incomplete data"
                               + (f" ({'; '.join(sorted(notes))})" if notes else "") + "; the sentence says so")
                continue
            failures.append(f"claim {c.claim_id} uses record language ('{word}') but cites incomplete fact(s) "
                            f"{', '.join(incomplete)}" + (f" ({'; '.join(sorted(notes))})" if notes else "")
                            + ": reword without record language, or drop it")
        elif not facts and m is not None:
            failures.append(f"claim {c.claim_id} uses record language ('{m.group(0)}') but cites no fact: cite the "
                            f"fact that establishes it or reword")

    # 6. names: a known policy or fund name in the prose must be a subject of a cited fact, or a name a
    # cited fact carries (its `names`, scope or rows, under any alias), or one the question itself used.
    if known_names is not None:
        index = known_names()
        alias_of = aliases() if aliases is not None else {}
        subjects = set(pack.subjects())
        held = _held_text(pack, cited, draft)
        if question:
            held += " | " + " ".join(question.casefold().replace(".", "").split())
        held_ids = _held_ids(pack, cited)
        for name, cid in _names_in(prose, index):
            key = " ".join(name.casefold().replace(".", "").split())
            if cid in subjects or cid in held_ids or key in held or key in pack.mentionable:
                continue
            if any(re.search(r"(?<![\w'])" + re.escape(a) + r"(?![\w'])", held) for a in alias_of.get(cid, ())):
                continue
            failures.append(f"'{name}' is named in the answer but no cited fact is about it: cite a fact "
                            f"about it or remove the name")

    # 7. disclosures
    for d in pack.disclosures():
        produced = [fid for fid, idx in pack.produced_by.items() if idx == d.call_index]
        if not any(f in cited for f in produced):
            continue
        if d.must_mention and d.must_mention.casefold() not in answer.casefold():
            failures.append(f"a default was applied ({d.text}) but the answer never says "
                            f"{d.must_mention!r}: state it")
        elif not d.must_mention and d.text not in answer:
            caveats.append(d.text)

    # 8. banned words: anywhere in the answer, whatever the facts say
    for word in _banned_hits(answer, banned):
        failures.append(f"the answer uses the banned word {word!r}: reword without it")

    for w in pack.warnings:
        caveats.append(w)
    if draft.unanswered:
        caveats.extend(f"Not answered: {u}" for u in draft.unanswered)

    if failures:
        return Verdict("BLOCKED", failures, caveats, len(found), len(found) - len(unbound), covered_by_evidence,
                       remap_candidates)
    return Verdict("VERIFIED_WITH_SCOPE" if caveats else "VERIFIED", [], caveats, len(found), len(found),
                   covered_by_evidence)


def _held_labels(pack: EvidencePack, cited: set[str]) -> set[tuple]:
    """Every year range, date and time a cited fact holds, as (kind, value) the prose tokens compare to."""
    out: set[tuple] = set()
    for f in cited:
        for fv in pack.fields(f).values():
            if fv.kind == "year_range":
                out.add(("year_range", range_start(fv.value)))
            elif fv.kind == "date":
                out.add(("date", date_iso(fv.value)))
            elif fv.kind == "time":
                out.add(("time", time_hhmm(fv.value)))
    return out


def _key(tok: Token) -> tuple:
    if tok.kind == "number":
        return ("number", round(tok.value, tok.places), tok.places, tok.percent)
    return (tok.kind, tok.value, 0, False)


def _figures_in_notes(pack: EvidencePack, cited: set[str]) -> list[float]:
    """Every number in the pack's warnings and the coverage notes of the cited facts."""
    texts = list(pack.warnings)
    for f in cited:
        reason = pack.facts[f].coverage.reason
        if reason:
            texts.append(reason)
    return [t.value for text in texts for t in tokens_in(text) if t.kind == "number"]


def _strings(value: Any, out: list[str], depth: int = 0) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict) and depth < 3:
        for v in value.values():
            _strings(v, out, depth + 1)
    elif isinstance(value, (list, tuple)) and depth < 3:
        for v in value[:25]:
            _strings(v, out, depth + 1)


_ID_FIELD = re.compile(r"(^|_)ids?$")


def _ids_in(value: Any, out: set[str], depth: int = 0) -> None:
    if isinstance(value, dict) and depth < 3:
        for k, v in value.items():
            if _ID_FIELD.search(str(k)):
                for x in (v if isinstance(v, (list, tuple)) else [v]):
                    if x not in (None, ""):
                        out.add(str(x))
            else:
                _ids_in(v, out, depth + 1)
    elif isinstance(value, (list, tuple)) and depth < 3:
        for v in value[:25]:
            _ids_in(v, out, depth + 1)


def _held_ids(pack: EvidencePack, cited: set[str]) -> set[str]:
    """Every identifier the cited facts carry: a policy the fact is demonstrably about even where the
    row spells its name differently."""
    out: set[str] = set()
    for f in cited:
        fact = pack.facts[f]
        _ids_in(fact.subject, out)
        _ids_in(fact.scope, out)
        _ids_in(fact.evidence_rows, out)
        _ids_in(fact.value, out)
    return out


def _held_text(pack: EvidencePack, cited: set[str], draft: AnswerDraft) -> str:
    """Every string the cited facts carry (labels, names, scope, row words, coverage wording, the cited
    insights' own text), casefolded and space-normalised, for the name check."""
    parts: list[str] = []
    for f in cited:
        fact = pack.facts[f]
        _strings(fact.subject, parts)
        _strings(fact.names, parts)
        _strings(fact.text, parts)
        _strings(fact.scope, parts)
        _strings(fact.evidence_rows, parts)
        _strings(fact.value, parts)
        parts.append(fact.coverage.requested or "")
    for c in draft.claims:
        for i in c.insight_ids:
            ins = pack.insights.get(i)
            if ins is not None:
                parts.extend([ins.headline, ins.premise])
                _strings(ins.subjects, parts)
    return " | ".join(" ".join(p.casefold().replace(".", "").split()) for p in parts if p)


_WORD_RUN = re.compile(r"[A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*)*")


def _names_in(answer: str, index: dict[str, str]) -> list[tuple[str, str]]:
    """Every full known name the prose contains: each capitalised run and every sub-run of it, looked up
    in the identity index. A word that is not a known full name is not a name."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _WORD_RUN.finditer(answer):
        words = m.group(0).split()
        for i in range(len(words)):
            for j in range(len(words), i, -1):
                phrase = " ".join(words[i:j])
                key = " ".join(phrase.casefold().replace(".", "").split())
                cid = index.get(key)
                if cid and key not in seen:
                    seen.add(key)
                    out.append((phrase, cid))
    return out


# --- remap: a zero-model fix for the one failure shape that is never ambiguous -------------------------

def remap(draft: AnswerDraft, verdict: Verdict, pack: EvidencePack, *,
          known_names: Optional[NameIndex] = None, aliases: Optional[AliasIndex] = None,
          question: str = "", banned: Iterable[str] = ()) -> Optional[tuple[AnswerDraft, Verdict]]:
    """A figure the pack holds somewhere, just not where the draft pointed, fixed by rewriting the
    pointer - never the prose - with no model call. All or nothing: applies only when EVERY failure has a
    remap candidate with exactly one holder; anything else goes to the model's repair."""
    if not verdict.remap_candidates or len(verdict.remap_candidates) != len(verdict.failures):
        return None
    for rc in verdict.remap_candidates:
        if len(rc.holders) != 1:
            return None
    fixed = draft.model_copy(deep=True)
    by_id = {c.claim_id: c for c in fixed.claims}
    for rc in verdict.remap_candidates:
        claim = by_id.get(rc.claim_id)
        if claim is None:
            return None
        fact_id, fieldname = rc.holders[0]
        if rc.kind == "binding":
            if rc.binding_index is None or rc.binding_index >= len(claim.bindings):
                return None
            old = claim.bindings[rc.binding_index]
            claim.bindings[rc.binding_index] = Binding(token=old.token, fact_id=fact_id, field=fieldname)
        else:
            claim.bindings.append(Binding(token=rc.token.text, fact_id=fact_id, field=fieldname))
        if fact_id not in claim.fact_ids:
            claim.fact_ids.append(fact_id)
    again = verify(fixed, pack, known_names=known_names, aliases=aliases, question=question, banned=banned)
    if again.status == "BLOCKED":
        return None
    return fixed, again


# --- rescue: drop what could not be verified, keep what could -------------------------------------------

_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")


def rescue(draft: AnswerDraft, verdict: Verdict, pack: EvidencePack, *,
           known_names: Optional[NameIndex] = None, aliases: Optional[AliasIndex] = None,
           question: str = "", banned: Iterable[str] = ()) -> Optional[tuple[AnswerDraft, Verdict]]:
    """A shorter draft with every sentence that carries a failed figure, name or banned word removed,
    re-verified. None when nothing survives or the remainder still fails. A number is never rewritten:
    only the sentence around it goes."""
    offending = set()
    for f in verdict.failures:
        m = re.search(r"^'(.+?)' (?:appears in the answer|is named in the answer)", f)
        if m:
            offending.add(m.group(1))
        m = re.search(r"^claim (\S+):", f) or re.search(r"^claim (\S+) ", f)
        if m:
            cid = m.group(1)
            for c in draft.claims:
                if c.claim_id == cid:
                    offending.add(c.text.strip())
        m = re.search(r"banned word '(.+?)'", f)
        if m:
            offending.add(m.group(1))
    if not offending:
        return None

    def tainted(text: str) -> bool:
        return any(o and o.casefold() in text.casefold() for o in offending)

    lines = []
    for line in draft.answer_markdown.splitlines():
        if line.lstrip().startswith("|") or line.lstrip().startswith(("-", "*", "#")):
            if not tainted(line):
                lines.append(line)
            continue
        kept = [s for s in _SENTENCE.findall(line) if not tainted(s)]
        lines.append(" ".join(s.strip() for s in kept))
    answer = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    claims = [c for c in draft.claims if not tainted(c.text) and c.text.strip() in answer]
    if not answer or not any(ch.isalpha() for ch in answer):
        return None
    shorter = draft.model_copy(update={"answer_markdown": answer, "claims": claims})
    again = verify(shorter, pack, known_names=known_names, aliases=aliases, question=question, banned=banned)
    if again.status == "BLOCKED":
        return None
    return shorter, again


# --- what the rep sees without a verified draft ----------------------------------------------------------

def _figure(value: Any) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        text = f"{value:,.2f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


_SHOWN_SCOPE = ("state", "family_type", "excess", "snapshot")


def quick_look(pack: EvidencePack, limit: int = 6) -> str:
    """What the facts say, as lines a rep can read straight off - shown the moment the facts exist,
    before the model has written a word. Real figures from real rows, formatted; never a sentence the
    model wrote, never arithmetic."""
    lines: list[str] = []
    for fact in pack.facts.values():
        if fact.value in (None, ""):
            continue
        if fact.text:
            lines.append(f"- {fact.text}")
        else:
            who = fact.subject.get("label") or ""
            what = (fact.metric or fact.type or "").replace("_", " ")
            value = fact.value
            if isinstance(value, dict):
                parts = [f"{k.replace('_', ' ')} {_figure(v)}" for k, v in value.items()
                         if not str(k).endswith("_id") and v not in (None, "")]
                if not parts:
                    continue
                body = f"{what}: " + ", ".join(parts[:6]) if what else ", ".join(parts[:6])
            elif fact.unit and fact.unit != "count":
                body = f"{_figure(value)} {fact.unit}"
            else:
                body = f"{_figure(value)}" + (f" {what}" if what else "")
            lines.append(f"- **{who}** — {body}" if who else f"- {body}")
        if len(lines) >= limit:
            break
    return "\n".join(lines)


def render_facts(pack: EvidencePack, limit: int = 40, prefer: Optional[list[str]] = None) -> str:
    """What the rep sees when a draft cannot be verified: each fact as one plain line (its own text when
    it has one). No internal syntax can reach the line. `prefer` (the facts the draft cited) come first."""
    lines: list[str] = []
    chosen = [pack.facts[f] for f in dict.fromkeys(prefer or []) if f in pack.facts]
    if not chosen:
        chosen = list(pack.facts.values())
    for fact in chosen[:limit]:
        if fact.text:
            lines.append(f"- {fact.text}")
            continue
        who = fact.subject.get("label") or ""
        what = (fact.metric or fact.type).replace("_", " ")
        value = fact.value
        if isinstance(value, dict):
            value = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in value.items() if not str(k).endswith("_id") and v not in (None, ""))
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            value = f"{value:,}" if isinstance(value, int) else f"{value:,.2f}".rstrip("0").rstrip(".")
        unit = f" {fact.unit}" if fact.unit and not isinstance(fact.value, dict) else ""
        scope = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in fact.scope.items()
                          if k in _SHOWN_SCOPE and v not in (None, ""))
        lines.append(f"- {who + ': ' if who else ''}{what} {value}{unit}" + (f" ({scope})" if scope else ""))
    for w in pack.warnings:
        lines.append(f"Note: {w}")
    return "\n".join(lines)
