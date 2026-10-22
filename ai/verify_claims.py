"""The claim-level verifier: is what the writer drafted something the cited facts say?

The lighter of the two verifiers. The writer returns sentences, each citing fact ids, with no binding of
each figure to a field (ai/verify.py is the binding one). It costs no extra output tokens, which is why
it exists beside the binding verifier: the plan's "sentence plus [F1,F3]" script is exactly this shape.
What it cannot catch is a figure held by a cited fact but on the wrong side of it (Navy Health's limit
stated as the competitor's) - both are in the same fact. Choose per route.

Deterministic, no model. A drafted answer is VERIFIED only if:

  1. it has at least one claim, and every claim cites at least one fact id or insight id the bundle
     holds; an insight resolves to exactly the facts it names, never the whole bundle;
  2. every number a claim states is one the claim's cited facts hold in their declared fields (value,
     occurrences, scope, subject, claimable, and the CLAIMABLE_EVIDENCE_KEYS of an evidence row), to the
     precision written: "$51" matches 51.0 or 51.4, "$51.40" does not match 51.0. Numbers written as words
     ("one thousand", "twice", "the fourth") are read as the numbers they are first;
  3. every number in the prose outside the claims is held by a fact some claim cites;
  4. every name a claim uses is one its cited facts hold: a capitalised name that does not appear in the
     cited facts fails the claim;
  5. no claim in absolute language (never, only, ever, most, highest, lowest, best, ...) rests on a fact
     whose coverage says complete_for_claim is False. The prose outside the claims is held to the same rule;
  6. no banned word appears anywhere in the answer.

Any failure is BLOCKED and names the claims that failed, unless dropping only the failed claims leaves a
smaller answer that verifies on its own (VERIFIED_PARTIAL). A pass with a caveat on the record is
VERIFIED_WITH_SCOPE.

What this does not check, written down: names are found by capitalisation, so a name typed in lower
case, an ALL-CAPS abbreviation and a single capitalised word that opens a sentence are not tested; "a
couple", "several" and "half" are not numbers to it; "first" and "second" are not read as ranks.
"""
from __future__ import annotations

import json
import re
from typing import Iterable, Optional

from .facts import CLAIMABLE_EVIDENCE_KEYS, AnswerWriterOutput, Claim, Fact, FactBundle, Insight, VerificationResult

_NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?:st|nd|rd|th)?(?![\w])")
_YEAR_RANGE = re.compile(r"(?<![\w.])(\d{4})\s*[-–/]\s*(\d{4}|\d{2})(?![\w])")
_DATE = re.compile(r"(?<![\w.])\d{4}-\d{2}-\d{2}(?!\d)")
_ABSOLUTE = re.compile(
    r"\b(never|first|only|ever|all[- ]time|of all time|in (?:\w+ ){0,2}history|"
    r"on record|record|most|fewest|least|highest|lowest|best|worst|greatest|biggest|largest|smallest|"
    r"longest|shortest|always|no one|nobody|every)\b", re.IGNORECASE)


# --- numbers written as words --------------------------------------------------------------------------------

_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * (i + 2) for i, w in enumerate(
    "twenty thirty forty fifty sixty seventy eighty ninety".split())}
_SCALES = {"hundred": 100, "thousand": 1_000, "million": 1_000_000}
_ORDINAL_UNITS = {"third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9,
                  "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
                  "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19}
_ORDINAL_TENS = {"twentieth": 20, "thirtieth": 30, "fortieth": 40, "fiftieth": 50, "sixtieth": 60,
                 "seventieth": 70, "eightieth": 80, "ninetieth": 90}
_TENS_THEN_ORDINAL = {"first": 1, "second": 2, **{w: v for w, v in _ORDINAL_UNITS.items() if v < 10}}
_ADVERBS = {"once": 1, "twice": 2, "thrice": 3}

_WORD = re.compile(r"[A-Za-z]+")
_ONCE_AS_A_CONJUNCTION = re.compile(r"\s+(he|she|they|it|the|we|i|you)\b", re.I)
# Words that open a lone "one" that is not a count: "one of", "one who", "no one", "the one".
_ONE_NOT_A_COUNT_AFTER = re.compile(
    r"\s+(of|another|who|whom|whose|that|which|can|could|would|should|will|is|was|were|has|had|might|may|"
    r"must|thing)\b", re.I)
_ONE_NOT_A_COUNT_BEFORE = re.compile(
    r"\b(no|any|every|each|some|the|this|that|which|what|at|one|another|not|only|for|and|but)\s+$", re.I)
_ORDINAL_OF_A_TIME = re.compile(r"[\s-]+(time|visit|year|month|week)\b", re.I)
# A number word naming how many of a GROUP the sentence is about ("the two policies", "these two funds")
# is a reference to who is being talked about, not a quantity claimed. The article or demonstrative
# straight before it matters: "compared two policies" alone still reads as a count.
_GROUP_NOUN_AFTER = re.compile(r"\s+(polic(?:y|ies)|funds?|sides?|options?|groups?|products?)\b", re.I)
_ARTICLE_OR_DEMONSTRATIVE_BEFORE = re.compile(r"\b(the|these|those)\s+$", re.I)
_DIGIT_GROUP_REFERENCE = re.compile(
    r"\b(the|these|those)\s+\d{1,3}\s+(polic(?:y|ies)|funds?|sides?|options?|groups?|products?)\b", re.I)
_MODIFIER = re.compile(r"-[A-Za-z]")                                        # "two-year", "one-off"
_NUMBER_AFTER_HYPHEN = re.compile(r"-(?:" + "|".join([*_UNITS, *_TENS, *_SCALES]) + r")\b", re.I)
_DECIMAL = re.compile(r"[\s-]+point((?:[\s-]+(?:zero|one|two|three|four|five|six|seven|eight|nine))+)\b", re.I)


def _read_number(words: list[str]) -> tuple[int, int]:
    """(value, words consumed) for the number that `words` opens; consumed is 0 if none does. Follows
    English: units and tens build up to 999, hundred multiplies it, thousand and million close a chunk,
    and "and" joins only after a scale ("one thousand and fifty", not "three and four")."""
    total = chunk = used = 0
    last = None                                   # unit | tens | tens_unit | hundred | scale
    i = 0
    while i < len(words):
        w = words[i]
        if w == "and":
            following = words[i + 1] if i + 1 < len(words) else None
            if last in ("hundred", "scale") and (following in _UNITS or following in _TENS):
                i += 1
                continue
            break
        if w in _UNITS:
            if last is None or last in ("hundred", "scale"):
                chunk += _UNITS[w]
                last = "unit"
            elif last == "tens" and 0 < _UNITS[w] < 10:
                chunk += _UNITS[w]
                last = "tens_unit"
            else:
                break
        elif w in _TENS:
            if last not in (None, "hundred", "scale"):
                break
            chunk += _TENS[w]
            last = "tens"
        elif w == "hundred":
            if last not in ("unit", "tens", "tens_unit") or not 0 < chunk < 100:
                break
            chunk *= 100
            last = "hundred"
        elif w in ("thousand", "million"):
            if last not in ("unit", "tens", "tens_unit", "hundred") or chunk <= 0:
                break
            total += chunk * _SCALES[w]
            chunk = 0
            last = "scale"
        else:
            break
        i += 1
        used = i
    return (total + chunk, used) if used else (0, 0)


def _joined(text: str, tokens: list[tuple[int, int, str]], a: int, b: int) -> bool:
    """True if only spaces or hyphens lie between word a and word b."""
    return b < len(tokens) and re.fullmatch(r"[\s-]+", text[tokens[a][1]:tokens[b][0]]) is not None


def _number_at(text: str, tokens: list[tuple[int, int, str]], i: int):
    """(digits, index of the next unread word, end offset in text) if a number written as words starts at
    word i and is a count, else None."""
    start, end, word = tokens[i]
    if start > 1 and text[start - 1] == "-" and text[start - 2].isalpha():
        return None                                # the tail of a compound ("one-on-one"), not a count
    if word in _ADVERBS:
        if word == "once" and (text[max(0, start - 3):start].lower().endswith("at ")
                               or _ONCE_AS_A_CONJUNCTION.match(text[end:])):
            return None
        return str(_ADVERBS[word]), i + 1, end
    if word in _ORDINAL_UNITS or word in _ORDINAL_TENS:
        if _ORDINAL_OF_A_TIME.match(text[end:]):
            return None
        return str(_ORDINAL_UNITS.get(word) or _ORDINAL_TENS[word]), i + 1, end

    lead = 0
    if word == "a" and i + 1 < len(tokens) and tokens[i + 1][2] in ("hundred", "thousand", "million") \
            and _joined(text, tokens, i, i + 1):
        lead = 1
    elif word not in _UNITS and word not in _TENS and word not in _SCALES:
        return None
    run = [tokens[i + lead][2]]
    j = i + lead
    while j + 1 < len(tokens) and len(run) < 10 and _joined(text, tokens, j, j + 1):
        j += 1
        run.append(tokens[j][2])
    if lead:
        run = ["one"] + run                       # "a hundred" reads as "one hundred"
    value, used = _read_number(run)
    if not used:
        return None
    last = i + used - 1
    last_end = tokens[last][1]
    following = tokens[last + 1][2] if _joined(text, tokens, last, last + 1) else None
    if (following in _TENS_THEN_ORDINAL and run[used - 1] in _TENS and value < 100):
        value += _TENS_THEN_ORDINAL[following]
        last += 1
        last_end = tokens[last][1]
    tail = text[last_end:]
    decimal = _DECIMAL.match(tail)
    if decimal:
        value_text = f"{value}." + "".join(str(_UNITS[w.lower()]) for w in re.findall(r"[A-Za-z]+", decimal.group(1)))
        last_end += decimal.end()
        while last + 1 < len(tokens) and tokens[last + 1][1] <= last_end:
            last += 1
        return value_text, last + 1, last_end
    if _MODIFIER.match(tail) and not _NUMBER_AFTER_HYPHEN.match(tail):
        return None                                # "two-year", "one-off"
    if used == 1 and run[0] == "one" and lead == 0 and (
            _ONE_NOT_A_COUNT_AFTER.match(tail) or _ONE_NOT_A_COUNT_BEFORE.search(text[max(0, start - 12):start])):
        return None                                # "one of", "no one"
    if (_GROUP_NOUN_AFTER.match(tail)
            and _ARTICLE_OR_DEMONSTRATIVE_BEFORE.search(text[max(0, start - 12):start])):
        return None                                # "the two policies" - not a count claim
    return str(value), last + 1, last_end


def spell_out_numbers(text: str) -> str:
    """The text with each number written as words replaced by its digits ("one thousand" -> "1000",
    "twice" -> "2", "twenty-fourth" -> "24", "twelve point five" -> "12.5"). Left as they were: a number
    word that modifies the next word ("two-year"), a lone "one" that is not a count ("one of"), an
    ordinal of a time ("third visit"), and "the two policies"."""
    tokens = [(m.start(), m.end(), m.group(0).lower()) for m in _WORD.finditer(text)]
    out: list[str] = []
    cursor = 0
    i = 0
    while i < len(tokens):
        hit = _number_at(text, tokens, i)
        if hit is None:
            i += 1
            continue
        digits, following, last_end = hit
        out.append(text[cursor:tokens[i][0]])
        out.append(digits)
        cursor = last_end
        i = following
    out.append(text[cursor:])
    return "".join(out)


# --- names -----------------------------------------------------------------------------------------------------

# Capitalised words that are not a policy's or a fund's name. A run of capitalised words is broken at
# each of these, so "In the Bupa Silver Plus policy" is the name "Bupa Silver Plus".
_NOT_NAMES = frozenset("""
a about above across after against all also although an and any are as at back based before between both but by
combined despite during each either every for from given had has have he her hers him his how however i
if in into is it its just like many meanwhile more most nevertheless no nonetheless not note of on once only or
other our over overall regardless she since so some still such than that the their them then there these they
this those though through to total under unless unlike until up was we were what when where whereas whether
which while who whose will with within without would yet you your
policy policies fund funds cover covers covered hospital extras premium premiums excess month monthly year yearly
annual limit limits benefit benefits waiting period periods member members family families single couple couples
state nsw vic qld sa wa tas nt act rebate discount
january february march april may june july august september october november december
monday tuesday wednesday thursday friday saturday sunday
""".split())
_WORDS = re.compile(r"[^\W\d_][\w'’\-]*")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n+")


def _folded(text: str) -> str:
    """Words only, lower case, one space between: what a name is compared as. An underscore is a space."""
    return " ".join(re.findall(r"[^\W_]+", str(text).casefold()))


def _held_names(facts: list[Fact]) -> str:
    """What the cited facts hold, folded, as words a name can be looked up in."""
    parts = [json.dumps(fact.model_dump(mode="json", exclude={"fact_id"}), default=str) for fact in facts]
    return f" {_folded(' '.join(parts))} "


def _held_as_segments(words: list[str], held: str) -> bool:
    """True if the words can be cut into runs the facts each hold whole: "Navy Health Bupa" is two
    funds. A single word the facts do not hold anywhere fails."""
    reachable = [True] + [False] * len(words)
    for end in range(1, len(words) + 1):
        for start in range(end):
            if reachable[start] and f" {_folded(' '.join(words[start:end]))} " in held:
                reachable[end] = True
                break
    return reachable[len(words)]


def _name_phrases(text: str) -> list[str]:
    """Runs of capitalised words that look like a name. A single capitalised word that opens a sentence
    is not returned; ALL-CAPS words are abbreviations and are not names."""
    found: list[str] = []
    for sentence in _SENTENCE_BREAK.split(text):
        run: list[re.Match] = []
        first_token_of_sentence = True

        def close(run: list[re.Match], opens_sentence: bool) -> None:
            if not run:
                return
            words = [re.sub(r"['’]s?$", "", m.group(0)) for m in run]
            if opens_sentence and len(words) == 1:
                return
            found.append(" ".join(words))

        opens = True
        for match in _WORDS.finditer(sentence):
            word = match.group(0)
            bare = re.sub(r"['’]s?$", "", word)
            is_name_word = (bare[:1].isupper() and not bare.isupper() and bare.casefold() not in _NOT_NAMES
                            and bare.casefold() not in {"s"})
            adjacent = bool(run) and sentence[run[-1].end():match.start()] == " "
            if is_name_word and (not run or adjacent):
                if not run:
                    opens = first_token_of_sentence
                run.append(match)
            else:
                close(run, opens)
                run = [match] if is_name_word else []
                if is_name_word:
                    opens = first_token_of_sentence
            first_token_of_sentence = False
        close(run, opens)
    return found


def _unheld_names(text: str, facts: list[Fact]) -> list[str]:
    held = _held_names(facts)
    return [name for name in dict.fromkeys(_name_phrases(text))
            if f" {_folded(name)} " not in held and not _held_as_segments(name.split(), held)]


# --- numbers, dates ------------------------------------------------------------------------------------------

def _numbers(text: str) -> list[tuple[float, int]]:
    """(value, decimal places) for each plain number in the text."""
    return [(float((m.group(1) + (m.group(2) or "")).replace(",", "")),
             len(m.group(2)) - 1 if m.group(2) else 0) for m in _NUMBER.finditer(text)]


def _claimable(fact: Fact) -> str:
    """What a fact lets a claim state as a number or date: its declared fields, and only the
    CLAIMABLE_EVIDENCE_KEYS of its evidence rows. The rest of an evidence row is provenance."""
    data = fact.model_dump(mode="json", exclude={"fact_id", "evidence_rows", "text", "source"})
    data["evidence"] = [{k: v for k, v in row.items() if k in CLAIMABLE_EVIDENCE_KEYS}
                        for row in fact.evidence_rows if isinstance(row, dict)]
    return json.dumps(data, default=str)


def _split(text: str) -> tuple[list[str], list[tuple[int, str]], str]:
    """(dates, (start year, end) ranges, and the text with both removed)."""
    dates = _DATE.findall(text)
    text = _DATE.sub(" ", text)
    ranges = [(int(m.group(1)), m.group(2)) for m in _YEAR_RANGE.finditer(text)]
    return dates, ranges, _YEAR_RANGE.sub(" ", text)


def _without_labels(text: str, facts: list[Fact]) -> str:
    """The text with a metric name that has a digit in it blanked out (an item code label, say)."""
    labels = {f.metric for f in facts if f.metric and re.search(r"\d", f.metric)}
    for label in labels:
        words = [w for w in re.split(r"[\s_\-]+", label) if w]
        text = re.sub(r"(?<![\w.])" + r"[\s_\-]+".join(re.escape(w) for w in words) + r"(?![\w])", " ", text,
                      flags=re.IGNORECASE)
    return text


def _without_digit_group_references(text: str) -> str:
    """`text` with "the 2 policies" reduced to "the policies": the digit is who the sentence is about."""
    return _DIGIT_GROUP_REFERENCE.sub(lambda m: f"{m.group(1)} {m.group(2)}", text)


def _problems(text: str, facts: list[Fact]) -> list[str]:
    """Why the numbers and dates in `text` are not all held by `facts`."""
    text = _without_digit_group_references(_without_labels(spell_out_numbers(text), facts))
    held = " ".join(_claimable(f) for f in facts)
    held_dates, held_ranges, held_rest = _split(held)
    held_years = {y for y, _ in held_ranges} | {int(e) for _, e in held_ranges if len(e) == 4}
    held_numbers = _numbers(held_rest) + [(float(y), 0) for y in held_years]
    dates, ranges, rest = _split(text)
    problems = [f"date {d} is not in the cited facts" for d in dates if d not in held_dates]
    for start, end in ranges:
        consistent = (int(end) == start + 1) if len(end) == 4 else (int(end) == (start + 1) % 100)
        if not (consistent and any(v == start for v, _ in held_numbers)):
            problems.append(f"{start}-{end} is not in the cited facts")
    for value, places in _numbers(rest):
        if not any(round(h, places) == value for h, _ in held_numbers):
            shown = f"{value:,.{places}f}"
            problems.append(f"the number {shown} is not in the cited facts")
    return list(dict.fromkeys(problems))


def _wording_problems(text: str, facts: list[Fact]) -> list[str]:
    """Names the cited facts do not hold."""
    return [f"it names {name!r}, which none of the cited facts is about" for name in _unheld_names(text, facts)]


def _resolve_claim_facts(claim: Claim, facts: dict[str, Fact],
                         insights: dict[str, Insight]) -> tuple[list[Fact], list[str]]:
    """The Facts claim.fact_ids and claim.insight_ids resolve to, and any id the bundle does not hold. An
    insight resolves to its own supporting facts (and recursively those of insights it cites) - never to
    the rest of the bundle."""
    resolved: dict[str, Fact] = {}
    missing: list[str] = []
    seen: set[str] = set()

    def add_insight(insight_id: str) -> None:
        if insight_id in seen:
            return
        seen.add(insight_id)
        insight = insights.get(insight_id)
        if insight is None:
            missing.append(insight_id)
            return
        for fid in insight.supporting_fact_ids:
            if fid in facts:
                resolved[fid] = facts[fid]
            else:
                missing.append(fid)
        for iid in insight.supporting_insight_ids:
            add_insight(iid)

    for fid in claim.fact_ids:
        if fid in facts:
            resolved[fid] = facts[fid]
        else:
            missing.append(fid)
    for iid in claim.insight_ids:
        add_insight(iid)
    return list(resolved.values()), missing


_LIST_NUMERAL = re.compile(r"^\s*\d{1,2}\.\s+")
_LIST_NUMERAL_ANY_LINE = re.compile(r"^\s*\d{1,2}\.\s+", re.MULTILINE)


def _sans_list_numeral(text: str) -> str:
    """`text` with a leading "1. " list marker removed: a position in a list, not a figure."""
    return _LIST_NUMERAL.sub("", text, count=1)


def _stray_text(answer: str, claims: list[Claim]) -> str:
    """The answer with every claim's own text taken out: the prose outside the claims. A claim's text
    that does not appear verbatim in the answer is not removed, so the check stays conservative."""
    text = answer
    for claim in claims:
        if claim.text and claim.text in text:
            text = text.replace(claim.text, " ", 1)
    return _LIST_NUMERAL_ANY_LINE.sub("", text)


_TIDY_BLANK_BULLET = re.compile(r"^[ \t]*[-*]\s*$", re.MULTILINE)
_TIDY_BLANK_LINES = re.compile(r"\n{3,}")
_TIDY_TRAILING_SPACE = re.compile(r"[ \t]+\n")
_TIDY_DOUBLE_SPACE = re.compile(r"[ \t]{2,}")


def _tidy_after_removal(text: str) -> str:
    """`text` after failed claims' sentences have been cut out: no empty bullet, no run of blank lines,
    no doubled space. Never removes anything a claim supplied."""
    text = _TIDY_TRAILING_SPACE.sub("\n", text)
    text = _TIDY_BLANK_BULLET.sub("", text)
    text = _LIST_NUMERAL_ANY_LINE.sub("", text)
    text = _TIDY_BLANK_LINES.sub("\n\n", text)
    text = _TIDY_DOUBLE_SPACE.sub(" ", text)
    return text.strip()


def _banned_hits(text: str, banned: Iterable[str]) -> list[str]:
    return [w.strip() for w in banned
            if w.strip() and re.search(r"(?<![\w'])" + re.escape(w.strip()) + r"(?![\w'])", text, re.I)]


def _partial_rescue(output: AnswerWriterOutput, bundle: FactBundle, failed: list[str],
                    banned: Iterable[str]) -> Optional[VerificationResult]:
    """When SOME, not all, claims failed their own check, try the answer with just the failed claims'
    text cut out, as its own smaller answer. A stray-text failure (an uncited figure in the prose outside
    every claim) never reaches here: it has no claim to remove."""
    bad_ids = {entry.split(":", 1)[0] for entry in failed}
    good_claims = [c for c in output.claims if c.claim_id not in bad_ids]
    if not good_claims or len(good_claims) == len(output.claims):
        return None
    reduced = output.answer
    for claim in output.claims:
        if claim.claim_id in bad_ids and claim.text and claim.text in reduced:
            reduced = reduced.replace(claim.text, "", 1)
    reduced = _tidy_after_removal(reduced)
    if not reduced:
        return None
    inner = verify_answer(AnswerWriterOutput(answer=reduced, claims=good_claims), bundle, banned=banned)
    if inner.status == "BLOCKED":
        return None
    dropped = len(output.claims) - len(good_claims)
    return VerificationResult(
        status="VERIFIED_PARTIAL", unverified_claims=failed,
        reason=f"{dropped} of {len(output.claims)} claim(s) could not be verified and were left out of "
               f"the answer; the rest verified" + (f" ({inner.reason})" if inner.reason else ""),
        partial_answer=reduced, kept_claim_ids=[c.claim_id for c in good_claims])


def verify_answer(output: AnswerWriterOutput, bundle: FactBundle, *, banned: Iterable[str] = ()) -> VerificationResult:
    facts = {f.fact_id: f for f in bundle.facts}
    insights = {i.insight_id: i for i in bundle.insights}
    banned = list(banned)
    if not output.answer.strip():
        return VerificationResult(status="BLOCKED", reason="the answer is empty")
    if not output.claims:
        return VerificationResult(status="BLOCKED", reason="the answer makes no claim that cites a fact")

    failed: list[str] = []
    incomplete: list[str] = []
    cited: dict[str, Fact] = {}
    for claim in output.claims:
        if not claim.fact_ids and not claim.insight_ids:
            failed.append(f"{claim.claim_id}: cites no fact or insight")
            continue
        support, missing = _resolve_claim_facts(claim, facts, insights)
        if missing:
            failed.append(f"{claim.claim_id}: cites unknown fact or insight id(s) {', '.join(missing)}")
            continue
        if not support:
            failed.append(f"{claim.claim_id}: cites an insight with no supporting fact in this bundle")
            continue
        cited.update({f.fact_id: f for f in support})
        text = _sans_list_numeral(claim.text)
        problems = _problems(text, support) + _wording_problems(text, support)
        problems += [f"it uses the banned word {w!r}" for w in _banned_hits(text, banned)]
        weak = [f for f in support if not f.coverage.complete_for_claim]
        if weak and _ABSOLUTE.search(text):
            problems.append("it makes an absolute claim (" + _ABSOLUTE.search(text).group(0).lower()
                            + ") on coverage that is not complete: "
                            + "; ".join(f.coverage.reason or f.coverage.requested for f in weak))
        if problems:
            failed.append(f"{claim.claim_id}: " + "; ".join(problems))
        incomplete += [f.coverage.reason or f.coverage.requested for f in weak]

    if failed:
        rescue = _partial_rescue(output, bundle, failed, banned)
        if rescue is not None:
            return rescue
        return VerificationResult(status="BLOCKED", unverified_claims=failed,
                                  reason=f"{len(failed)} problem(s) found in the drafted answer")

    stray_text = _stray_text(output.answer, output.claims)
    stray = _problems(stray_text, list(cited.values())) + _wording_problems(stray_text, list(cited.values()))
    stray += [f"it uses the banned word {w!r}" for w in _banned_hits(stray_text, banned)]
    weak_cited = [f for f in cited.values() if not f.coverage.complete_for_claim]
    if weak_cited and _ABSOLUTE.search(stray_text):
        stray.append("it makes an absolute claim (" + _ABSOLUTE.search(stray_text).group(0).lower()
                     + ") on coverage that is not complete: "
                     + "; ".join(f.coverage.reason or f.coverage.requested for f in weak_cited))
    if stray:                                     # no single claim to blame, so nothing here is rescuable
        failed = [f"answer: {p}" for p in stray]
        return VerificationResult(status="BLOCKED", unverified_claims=failed,
                                  reason=f"{len(failed)} problem(s) found in the drafted answer")

    caveats = list(dict.fromkeys([*incomplete, *bundle.coverage_warnings]))
    if caveats:
        return VerificationResult(status="VERIFIED_WITH_SCOPE", reason="; ".join(caveats))
    return VerificationResult(status="VERIFIED")
