"""Near-miss scoring for policy and fund names an exact or alias lookup could not match.

This only *scores*. Whether a near miss may be acted on is decided by the caller (the rep search, the
identity resolver) under one rule: act only on a clear, unambiguous winner, and otherwise offer the
candidates - never pick between two. Nothing here consults a model, a database or a canonical id beyond
the ones it is handed.

Distances are on "compact" names (lower-case, accents folded, hyphens and spacing gone) so
"Top-Hospital" and "Top Hospital" are the same string before any spelling is compared.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Optional

_SUFFIXES = {"jr", "jnr", "sr", "snr", "ii", "iii", "iv"}

# The compact length a name needs before a slip of 1, 2 or 3 letters may be
# forgiven automatically. Short names have too few letters to tell a typo from
# a different name ("Gold Plus" / "Gold Plu").
AUTO_MIN_LENGTH = {1: 9, 2: 14, 3: 20}
AUTO_MIN_TOKENS = 2
# A second candidate this close to the best (in letters) means the best is not
# clearly ahead.
CLOSE_SECOND = 2
SUGGEST_MAX = 5


def norm(value: Any) -> str:
    """"Silver-Plus  Advanced." -> "silver plus advanced"."""
    s = unicodedata.normalize("NFKD", str(value or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[-_]", " ", s)
    s = re.sub(r"[^\w\s]", "", s)
    return " ".join(s.split())


def tokens(value: Any) -> list[str]:
    return [t for t in norm(value).split() if t not in _SUFFIXES]


def compact(value: Any) -> str:
    return "".join(tokens(value))


def distance(a: str, b: str, cap: int) -> int:
    """Optimal-string-alignment distance (a swap of neighbours counts once),
    or cap + 1 as soon as it is certain to exceed `cap`."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2: Optional[list[int]] = None
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > cap:
            return cap + 1
        prev2, prev = prev, cur
    return min(prev[-1], cap + 1)


def allowed_slip(length: int) -> int:
    """The most letters a name of this compact length may be off by and still be
    offered as a suggestion (about one in five)."""
    return max(1, round(length * 0.2))


@dataclass(frozen=True)
class Match:
    canonical_id: str
    identity: Any
    matched: str          # the known name it is close to
    slip: int             # letters off
    score: float          # 1 - slip / longer length
    kind: str             # "name" | "partial"


def best_matches(query: str, known: Iterable[tuple[str, Any]], id_of, kind: str = "name") -> list[Match]:
    """Every known name within reach of `query`, one per canonical id (its
    closest name), closest first. `known` is (name, identity) pairs; `id_of`
    reads an identity's canonical id. Names whose first letter differs are
    skipped: a typo rarely lands on the first character, and it keeps a
    different policy out of the list."""
    q = compact(query)
    if len(q) < 5:
        return []
    # Look CLOSE_SECOND letters past what may be suggested, so a second policy
    # that is nearly as close is seen by `safe_auto_match` even though no one
    # would suggest it.
    reach = (allowed_slip(len(q)) if kind == "name" else 1) + CLOSE_SECOND
    best: dict[str, Match] = {}
    for name, identity in known:
        k = compact(name)
        if not k or k[0] != q[0]:
            continue
        d = distance(q, k, reach)
        if d > reach or d == 0:
            continue
        cid = id_of(identity)
        m = Match(cid, identity, name, d, round(1 - d / max(len(q), len(k)), 3), kind)
        if cid not in best or (m.slip, m.matched) < (best[cid].slip, best[cid].matched):
            best[cid] = m
    return sorted(best.values(), key=lambda m: (m.slip, m.matched, m.canonical_id))


def suggestions(query: str, matches: list[Match]) -> list[Match]:
    """The matches close enough to offer as "did you mean", at most SUGGEST_MAX."""
    q = compact(query)
    return [m for m in matches
            if m.slip <= (allowed_slip(len(q)) if m.kind == "name" else 1)][:SUGGEST_MAX]


def safe_auto_match(query: str, matches: list[Match], *, min_tokens: int = AUTO_MIN_TOKENS,
                     given_name_confirmed: bool = False) -> Optional[Match]:
    """The one match that may be acted on without asking, or None.

    All of: the name has at least `min_tokens` words, a slip small enough to
    be a typo rather than a different name (its letters-off count is one of
    AUTO_MIN_LENGTH's, 1-3), the closest by a clear margin over every other
    candidate, and a full-name match (never a partial-name match).

    `min_tokens` is 2 for a policy name (a single word like "Gold" is never
    auto-corrected) and 1 for a fund, whose whole name may be one word
    ("Medibank"); a small, closed vocabulary makes the margin check the
    safeguard there instead of a length floor.

    `given_name_confirmed` relaxes the length floor when the query's first
    word is already, independently of this match, the closest candidate's
    first word exactly (the fund name, "Bupa" of "Bupa Silver Plus") - the
    rest is then the only thing left that could be a typo."""
    if not matches:
        return None
    best = matches[0]
    q = compact(query)
    if best.kind != "name" or len(tokens(query)) < min_tokens:
        return None
    if best.slip not in AUTO_MIN_LENGTH:
        return None
    if any(m.slip <= best.slip + CLOSE_SECOND for m in matches[1:]):
        return None
    if min_tokens == 1 or given_name_confirmed:
        return best
    if len(q) < AUTO_MIN_LENGTH[best.slip]:
        return None
    return best


def merge(*groups: Iterable[Match]) -> list[Match]:
    """Several `best_matches` results on the same query, combined as one -
    kept once per canonical id (its closer match), ordered as `best_matches`
    itself would. Needed whenever a query is scored against more than one
    vocabulary (a policy's full names and its aliases): without this, the
    same policy counted once per list could look like a second, competing
    candidate to `safe_auto_match`."""
    best: dict[str, Match] = {}
    for group in groups:
        for m in group:
            prior = best.get(m.canonical_id)
            if prior is None or (m.slip, m.matched) < (prior.slip, prior.matched):
                best[m.canonical_id] = m
    return sorted(best.values(), key=lambda m: (m.slip, m.matched, m.canonical_id))
