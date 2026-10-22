"""The Fact contract, field-level provenance over it, and the EvidencePack a call gathers.

A Fact says what it establishes in `value`, `occurrences`, `claimable`, `scope` and the
CLAIMABLE_EVIDENCE_KEYS of its evidence rows. The verifier (ai/verify.py) names every citable figure as
a field - "value.navy", "value.comp", "claimable.rank", "scope.state", "rows[0].benefit_amt" - and the
model binds each number it writes to one of those names. That is what stops the one error a rep must
never repeat to a member: a Navy Health figure stated as the competitor's, or the reverse. Both live in
one Fact; they are different fields.

Every model forbids unknown fields. Output from a language model with a field it invented is output
nothing downstream reads: better to reject it than to act on what the model thought it had said.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- the contracts ----------------------------------------------------------------------------------

class CoverageMeta(_Contract):
    """What the source holds against what was asked. complete_for_claim is the one the verifier reads:
    False means no claim of "never", "only" or "most" may rest on this."""
    requested: str
    available_from: Optional[str] = None
    available_to: Optional[str] = None
    complete_for_claim: bool
    reason: Optional[str] = None


# The only keys of an evidence row a claim may take a figure from. Evidence rows are provenance: the
# source row a fact was read off, holding many figures that are not what the fact says. These are the
# ones that are part of it: where it ranks, out of how many, when, and which snapshot, state and family
# type it belongs to. Any other figure on the row (another excess level's premium, another service's
# limit) is exactly the incidental number a claim must not pick up.
CLAIMABLE_EVIDENCE_KEYS = frozenset({
    "rank", "of", "date", "snapshot", "effective", "state", "family_type", "excess"})


class Fact(_Contract):
    """One thing a comparison establishes, in a form a claim can cite by id.

    `value` is a figure, or a dict of figures by side: {"navy": 1000, "comp": 600}. `names` are the
    proper nouns a claim citing this fact may use (both policy names, the item); `unit` applies to the
    value and to each side of a value dict ("AUD", "AUD/month", "months", "percent")."""
    fact_id: str
    type: str
    subject: dict
    metric: Optional[str] = None
    operation: Optional[str] = None
    value: Any
    unit: Optional[str] = None
    text: Optional[str] = None
    names: list[str] = Field(default_factory=list)
    occurrences: Optional[int] = None
    scope: dict = Field(default_factory=dict)
    # Further figures that are part of this fact and may be claimed, named by what they are.
    claimable: dict = Field(default_factory=dict)
    evidence_rows: list[Any] = Field(default_factory=list)
    source: dict = Field(default_factory=dict)
    coverage: CoverageMeta


class Insight(_Contract):
    """A deterministic reading of one or more Facts, never a new number of its own: everything it states
    is already inside the Facts named in supporting_fact_ids. `headline` is the conclusion code has
    already worked out; `premise` the one-sentence reasoning behind it, naming the actual figures."""
    insight_id: str
    type: str
    headline: str
    premise: str
    subjects: list[dict] = Field(default_factory=list)
    supporting_fact_ids: list[str] = Field(default_factory=list, min_length=1)
    supporting_insight_ids: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "high"


class FactBundle(_Contract):
    bundle_id: str
    facts: list[Fact]
    coverage_warnings: list[str] = Field(default_factory=list)
    generated_at: str
    insights: list[Insight] = Field(default_factory=list)


class Claim(_Contract):
    claim_id: str
    text: str
    fact_ids: list[str]
    insight_ids: list[str] = Field(default_factory=list)


class AnswerWriterOutput(_Contract):
    answer: str
    claims: list[Claim]


class VerificationResult(_Contract):
    # VERIFIED_PARTIAL: some, not all, of the draft's claims failed; `partial_answer` is the answer with
    # only the failed claims' own text removed, re-verified as its own smaller answer.
    status: Literal["VERIFIED", "VERIFIED_WITH_SCOPE", "VERIFIED_PARTIAL", "BLOCKED"]
    unverified_claims: list[str] = Field(default_factory=list)
    reason: Optional[str] = None
    partial_answer: Optional[str] = None
    kept_claim_ids: list[str] = Field(default_factory=list)


# --- fields: every citable figure of a Fact, by name -------------------------------------------------

# Off by default: when on, a Fact gets a human-readable alias ("bupa_silver_plus.general_dental.nsw")
# the model may read and write instead of its id - never a replacement for fact_id itself.
SEMANTIC_KEYS_ENABLED = os.environ.get("AI_SEMANTIC_KEYS_ENABLED", "0") == "1"
_SLUG_RE = re.compile(r"[^a-z0-9]+")

FieldKind = Literal["number", "year_range", "date", "time", "score", "text"]

_YEAR_RANGE = re.compile(r"^\s*(\d{4})\s*[-/]\s*(\d{2}|\d{4})\s*$")
_DATE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")
_TIME = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
# A pair of figures as one: "97-93" as text, or [97, 93].
_SCORE = re.compile(r"^\s*(\d{1,3})\s*[-–]\s*(\d{1,3})\s*$")
_NUMERIC_STRING = re.compile(r"^\s*-?\d+(\.\d+)?\s*$")
_PERCENT_KEY = re.compile(r"(pct|percent|percentage|_share|share_pct|rate)$", re.I)
_ROWS_IN_FIELDS = 25          # evidence rows whose claimable keys become fields
_ROWS_SHOWN = 10              # evidence rows shown to the model, redacted
_ROWS_COUNTED_UP_TO = 20      # fewer rows than this is a complete set, so its count is a figure
_ID_KEY = re.compile(r"(^|_)id$")
# Scope keys that are bookkeeping, never something a reader is told as a figure or a name.
_HIDDEN_SCOPE = frozenset({"entity", "measure", "excludes", "section", "snapshot_id"})
_UNIT_BY_KEY = {"rank": "rank", "of": "count", "occurrences": "count", "sample": "count",
                "population": "count", "missing": "count"}
# A condition written into scope as text ("premium_diff >= 30"): the threshold is a figure the answer
# may restate ("more than $30 a month"), so it is a field (scope.<key>.value), not a stray number.
_CONDITION_TEXT = re.compile(r"^\s*[\w.]+\s*(==|>=|<=|!=|>|<)\s*(-?\d+(?:\.\d+)?)\s*$")


@dataclass(frozen=True)
class FieldValue:
    """One citable figure of one Fact: its value, what it is measured in, and what kind of token in prose
    may bind to it (a number, a date)."""
    value: Any
    unit: Optional[str]
    kind: FieldKind


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_score_pair(value: Any) -> bool:
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and all(_is_number(v) and float(v).is_integer() and 0 <= v < 1000 for v in value))


def _classify(value: Any) -> Optional[FieldKind]:
    if _is_number(value):
        return "number"
    if isinstance(value, str):
        if _YEAR_RANGE.match(value):
            return "year_range"
        if _DATE.match(value):
            return "date"
        if _TIME.match(value):
            return "time"
        if _NUMERIC_STRING.match(value):            # an item code "012": a figure stored as text
            return "number"
        if _SCORE.match(value):
            return "score"
    if _is_score_pair(value):
        return "score"
    return None


def score_parts(value: Any) -> Optional[tuple[float, float]]:
    """The two figures of a pair field, however it is stored."""
    if _is_score_pair(value):
        return float(value[0]), float(value[1])
    if isinstance(value, str):
        m = _SCORE.match(value)
        if m:
            return float(m.group(1)), float(m.group(2))
    return None


def as_number(value: Any) -> Optional[float]:
    if _is_number(value):
        return float(value)
    if isinstance(value, str) and _NUMERIC_STRING.match(value):
        return float(value)
    return None


def time_hhmm(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    m = _TIME.match(value)
    return f"{int(m.group(1))}:{m.group(2)}" if m else None


def range_start(value: Any) -> Optional[int]:
    """The start year a financial-year label names ("2025-26", "2025/2026"), or None."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return None
    m = _YEAR_RANGE.match(value)
    return int(m.group(1)) if m else None


def date_iso(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    m = _DATE.match(value)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _add(out: dict[str, FieldValue], name: str, value: Any, unit: Optional[str]) -> None:
    kind = _classify(value)
    if kind is None:
        return
    if kind == "number" and unit is None and _PERCENT_KEY.search(name.rsplit(".", 1)[-1]):
        unit = "percent"                    # benefit_pct: a percentage by name
    if kind == "score" and not isinstance(value, str):
        value = f"{int(value[0])}-{int(value[1])}"
    out[name] = FieldValue(value, unit, kind)


def fields_of(fact: Fact) -> dict[str, FieldValue]:
    """Every figure of `fact` a claim may state, by field name. The names are stable: the model is shown
    them and binds to them, and the verifier reads them back the same way."""
    out: dict[str, FieldValue] = {}
    unit = fact.unit
    if unit is None and fact.metric and _PERCENT_KEY.search(fact.metric):
        unit = "percent"
    _add(out, "value", fact.value, unit)
    if isinstance(fact.value, dict):
        for k, v in fact.value.items():
            if _ID_KEY.search(str(k)) or k == "unit":
                continue
            # Each side of a {"navy": .., "comp": ..} value is in the fact's own unit.
            _add(out, f"value.{k}", v, _UNIT_BY_KEY.get(k, unit))
    _add(out, "occurrences", fact.occurrences, "count")
    for k, v in fact.claimable.items():
        _add(out, f"claimable.{k}", v, _UNIT_BY_KEY.get(k))
    for k, v in fact.scope.items():
        if k in _HIDDEN_SCOPE:
            continue
        if isinstance(v, dict):
            for kk, vv in v.items():
                if not _ID_KEY.search(str(kk)):
                    _add(out, f"scope.{k}.{kk}", vv, _UNIT_BY_KEY.get(kk))
            continue
        if isinstance(v, str) and _classify(v) is None:
            m = _CONDITION_TEXT.match(v)
            if m:
                _add(out, f"scope.{k}.value", float(m.group(2)) if "." in m.group(2) else int(m.group(2)), None)
            continue
        _add(out, f"scope.{k}", v, _UNIT_BY_KEY.get(k))
    for i, row in enumerate(fact.evidence_rows[:_ROWS_IN_FIELDS]):
        if not isinstance(row, dict):
            continue
        for k in CLAIMABLE_EVIDENCE_KEYS:
            if k in row:
                _add(out, f"rows[{i}].{k}", row[k], _UNIT_BY_KEY.get(k))
    if 0 < len(fact.evidence_rows) < _ROWS_COUNTED_UP_TO:
        _add(out, "rows.count", len(fact.evidence_rows), "count")
    return out


def _redact_row(row: Any) -> Any:
    """An evidence row as the model may see it: the claimable keys, and the words that say which row it
    is. Any other figure is left out - it is not claimable, so showing it would only tempt a number the
    verifier must then reject."""
    if not isinstance(row, dict):
        return row
    kept = {}
    for k, v in row.items():
        if k in CLAIMABLE_EVIDENCE_KEYS or isinstance(v, bool) or (isinstance(v, str) and not _classify(v) == "number"):
            kept[k] = f"{int(v[0])}-{int(v[1])}" if _is_score_pair(v) else v
    return kept


def _ids(fact: Fact) -> dict[str, Any]:
    """Identifiers shown apart from the fields, because an id is never a figure to state."""
    ids: dict[str, Any] = {}
    if isinstance(fact.value, dict):
        for k, v in fact.value.items():
            if _ID_KEY.search(str(k)) and v not in (None, ""):
                ids[k] = v
    for row in fact.evidence_rows[:1]:
        if isinstance(row, dict):
            for k, v in row.items():
                if _ID_KEY.search(str(k)) and v not in (None, "") and k not in ids:
                    ids[k] = v
    if fact.subject.get("id"):
        ids["subject_id"] = fact.subject["id"]
    return ids


def _slug(text: Any) -> str:
    return _SLUG_RE.sub("_", str(text).strip().casefold()).strip("_") or "x"


def semantic_key(fact: Fact) -> str:
    """A human-readable alias for fact.fact_id: subject, metric, then the scope a reader would be told
    (state, family type)."""
    parts: list[str] = []

    def add(part: Optional[str]) -> None:
        if part and (not parts or parts[-1] != part):
            parts.append(part)

    subject = fact.subject.get("label")
    if subject:
        add(_slug(subject))
    metric = fact.metric or fact.operation or fact.type
    if metric:
        add(_slug(metric))
    for key in ("state", "family_type"):
        v = fact.scope.get(key)
        if v:
            add(_slug(v))
    return ".".join(parts) if parts else _slug(fact.fact_id)


def compact_fact(fact: Fact, *, id: Optional[str] = None) -> dict:
    """The Fact as the model is shown it: id, what it is about, its citable fields, its scope, the names
    it may use, and whether a claim of completeness may rest on it."""
    shown = {
        "id": id or fact.fact_id,
        "type": fact.type,
        "text": fact.text,
        "subject": {k: fact.subject.get(k) for k in ("label", "entity_type") if fact.subject.get(k) is not None},
        "metric": fact.metric,
        "unit": fact.unit,
        "fields": {name: fv.value for name, fv in fields_of(fact).items()},
        "names": list(fact.names),
        "scope": {k: v for k, v in fact.scope.items() if k not in _HIDDEN_SCOPE and v not in (None, "", [])},
        "complete": fact.coverage.complete_for_claim,
    }
    if fact.operation:
        shown["operation"] = fact.operation
    if not fact.coverage.complete_for_claim and fact.coverage.reason:
        shown["coverage_note"] = fact.coverage.reason
    rows = [_redact_row(r) for r in fact.evidence_rows[:_ROWS_SHOWN]]
    rows = [r for r in rows if r]
    if rows:
        shown["rows"] = rows
        if len(fact.evidence_rows) > _ROWS_SHOWN:
            shown["rows_note"] = f"first {_ROWS_SHOWN} of {len(fact.evidence_rows)} rows shown"
    ids = _ids(fact)
    if ids:
        shown["ids"] = ids
    return {k: v for k, v in shown.items() if v not in (None, {}, [])}


def compact_insight(insight: Insight) -> dict:
    return {"id": insight.insight_id, "type": insight.type, "headline": insight.headline,
            "premise": insight.premise, "supporting_fact_ids": list(insight.supporting_fact_ids),
            "confidence": insight.confidence}


# --- the evidence one AI call is given ---------------------------------------------------------------

@dataclass
class Disclosure:
    """A default the code applied because the question did not say (a family type assumed, the data
    date): the answer must say it. When `must_mention` is set, the answer has to contain it."""
    text: str
    must_mention: Optional[str]
    source: str
    call_index: int


@dataclass
class QueryCall:
    """One query that put facts in the pack (a question type's SQL, the card read)."""
    index: int
    name: str
    args: dict
    status: str = "ok"
    ms: int = 0
    fact_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None
    disclosure: Optional[Disclosure] = None


@dataclass
class EvidencePack:
    """Every Fact the call's queries returned, by id, with which query produced it and what that query
    disclosed. The verifier reads bindings against this; the fallback renders from it."""
    facts: dict[str, Fact] = field(default_factory=dict)
    insights: dict[str, Insight] = field(default_factory=dict)
    calls: list[QueryCall] = field(default_factory=list)
    produced_by: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # Names a lookup returned (a policy search's candidates): evidence the policy exists, so an answer
    # may name it (to ask which one is meant) without a figure about it.
    mentionable: set[str] = field(default_factory=set)
    display_id: dict[str, str] = field(default_factory=dict)
    by_display: dict[str, str] = field(default_factory=dict)
    _key_seq: dict[str, int] = field(default_factory=dict)

    @classmethod
    def of(cls, facts: list[Fact], *, name: str = "card", warnings: Optional[list[str]] = None) -> "EvidencePack":
        """A pack holding `facts` from one query - the common case: the card's facts, ready to write from."""
        pack = cls()
        call = QueryCall(index=0, name=name, args={})
        pack.add_call(call)
        pack.add_facts(facts, call)
        for w in warnings or []:
            pack.warnings.append(w)
        return pack

    def add_call(self, call: QueryCall) -> None:
        self.calls.append(call)

    def allow_name(self, name: Any) -> None:
        if isinstance(name, str) and name.strip():
            self.mentionable.add(" ".join(name.casefold().replace(".", "").split()))

    def add_facts(self, facts: list[Fact], call: QueryCall) -> list[str]:
        ids = []
        for f in facts:
            held = self.facts.get(f.fact_id)
            if held is not None and self.produced_by.get(f.fact_id) != call.index and held != f:
                # A different query returned a different fact under the same id. Both must survive: a
                # binding to the first must not silently read the second.
                f = f.model_copy(update={"fact_id": f"{f.fact_id}.c{call.index}"})
            self.facts[f.fact_id] = f
            self.produced_by.setdefault(f.fact_id, call.index)
            ids.append(f.fact_id)
            if SEMANTIC_KEYS_ENABLED:
                self._assign_display_id(f)
        call.fact_ids.extend(i for i in ids if i not in call.fact_ids)
        return ids

    def add_insights(self, insights: list[Insight]) -> None:
        for ins in insights:
            self.insights[ins.insight_id] = ins

    def _assign_display_id(self, fact: Fact) -> None:
        if fact.fact_id in self.display_id:
            return
        key = semantic_key(fact)
        if self.by_display.get(key, fact.fact_id) != fact.fact_id:
            self._key_seq[key] = self._key_seq.get(key, 1) + 1
            key = f"{key}.{self._key_seq[key]}"
        self.display_id[fact.fact_id] = key
        self.by_display[key] = fact.fact_id

    def resolve_id(self, id_or_key: str) -> str:
        """A binding's fact_id, however the model wrote it: the real id, a semantic key to the id it
        aliases, or anything else unchanged (so a bad id still fails the citation check)."""
        if id_or_key in self.facts:
            return id_or_key
        return self.by_display.get(id_or_key, id_or_key)

    def fields(self, fact_id: str) -> dict[str, FieldValue]:
        fact = self.facts.get(self.resolve_id(fact_id))
        return fields_of(fact) if fact is not None else {}

    def disclosures(self) -> list[Disclosure]:
        return [c.disclosure for c in self.calls if c.disclosure is not None]

    def subjects(self) -> dict[str, dict]:
        """Every subject a held Fact is about, by canonical id."""
        out: dict[str, dict] = {}
        for f in self.facts.values():
            sid = f.subject.get("id")
            if sid:
                out.setdefault(str(sid), f.subject)
            for row in f.evidence_rows[:_ROWS_IN_FIELDS]:
                if isinstance(row, dict):
                    for key, name in (("policy_id", "policy_name"), ("fund_code", "fund_name")):
                        if row.get(key) and row.get(name):
                            out.setdefault(str(row[key]), {"id": row[key], "label": row[name]})
        return out

    def complete(self, fact_id: str) -> bool:
        fact = self.facts.get(self.resolve_id(fact_id))
        return bool(fact and fact.coverage.complete_for_claim)
