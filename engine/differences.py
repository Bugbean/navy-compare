"""Which policy is better, benefit by benefit.

Two views, both read from the Values in engine/benefits.py:

  card(navy, comp)   one Navy Health policy against one competitor. Every benefit gets a direction
                     (navy_better, comp_better, same, trade_off, different, missing), a size, and a
                     materiality score from config/materiality.yaml, and the card lists where Navy
                     Health is stronger and what to watch out for, most material first.

  matrix(policies)   any number of policies side by side. Each row marks the policy or policies
                     with the best value, where the benefit can be ranked at all.

Nothing here invents a judgement the data does not support. A benefit whose two values cannot be
ranked (a limit per person against one per policy, two kinds of accommodation) is "different", and
the excess is always a trade-off: a lower excess comes with a higher premium.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from config.features import FEATURES, GROUPS
from engine import materiality as mat
from engine.benefits import UNLIMITED, Value

NAVY_BETTER, COMP_BETTER, SAME, TRADE_OFF, DIFFERENT, MISSING = (
    "navy_better", "comp_better", "same", "trade_off", "different", "missing")

# The rows a matrix always shows; other benefits show when they differ or were asked for.
CORE_ROWS = ("price", "excess", "copayment", "tier", "accommodation", "wait:PreExisting", "wait:Other",
             "wait:SubAcute", "wait:PregnancyBirth", "ambulance")


def comparable(a: Value, b: Value) -> bool:
    if a.rank is None or b.rank is None:
        return False
    return a.basis == b.basis or "any" in (a.basis, b.basis)


def _size(a: Value, b: Value) -> Optional[float]:
    if a.size is None or b.size is None:
        return None
    if UNLIMITED in (a.size, b.size):
        return UNLIMITED if a.size != b.size else 0.0
    return abs(a.size - b.size)


@dataclass
class Difference:
    key: str
    label: str
    group: str
    kind: str
    navy: Optional[Value]
    comp: Optional[Value]
    direction: str
    size: Optional[float] = None
    materiality: int = 0
    tags: set[str] = field(default_factory=set)
    chip_match: bool = False


def compare_pair(key: str, navy: Optional[Value], comp: Optional[Value], cfg: Optional[dict] = None) -> Difference:
    f = FEATURES.get(key)
    kind = (navy or comp).kind
    d = Difference(key=key, label=f.label if f else key, group=f.group if f else "", kind=kind,
                   navy=navy, comp=comp, direction=SAME, tags=mat.tags_for(key, cfg))
    if navy is None or comp is None:
        d.direction = MISSING
        return d
    if navy.display == comp.display and navy.note == comp.note:
        return d
    if kind == "wait_months" and (navy.rank is None or comp.rank is None):
        # One side does not cover what this wait is for; the clinical category row already says so.
        d.direction = MISSING
        return d
    if kind == "excess":
        d.direction = TRADE_OFF if navy.size != comp.size else SAME
        d.size = _size(navy, comp)
        d.materiality = mat.score("excess", d.size, cfg) if d.direction == TRADE_OFF else 0
        return d
    if not comparable(navy, comp) or navy.rank == comp.rank:
        d.direction = DIFFERENT if navy.display != comp.display else SAME
        d.materiality = 1 if d.direction == DIFFERENT else 0
        return d
    d.size = _size(navy, comp)
    d.direction = NAVY_BETTER if navy.rank > comp.rank else COMP_BETTER
    size_for_score = 999999.0 if d.size == UNLIMITED else d.size
    d.materiality = mat.score(kind, size_for_score, cfg)
    if d.materiality == 0:
        d.direction = SAME   # below the noise threshold: not a difference worth saying
    return d


def _order(diffs: Iterable[Difference], chips: set[str], cfg: Optional[dict]) -> list[Difference]:
    out = list(diffs)
    for d in out:
        d.chip_match = bool(d.tags & chips)
    return sorted(out, key=lambda d: (-d.materiality, not d.chip_match, mat.priority(d.kind, cfg), d.label))


@dataclass
class Card:
    navy_id: int
    comp_id: int
    stronger: list[Difference]
    watch: list[Difference]
    trade_offs: list[Difference]
    same: list[Difference]
    missing: list[Difference]
    price: Optional[Difference]


def card(navy_id: int, comp_id: int, vals: dict[int, dict[str, Value]], *, keys: Optional[list[str]] = None,
         chips: Iterable[str] = (), cfg: Optional[dict] = None) -> Card:
    """One Navy Health policy against one competitor, ranked."""
    chips = set(chips)
    n, c = vals.get(navy_id, {}), vals.get(comp_id, {})
    wanted = keys or sorted(set(n) | set(c))
    diffs = [compare_pair(k, n.get(k), c.get(k), cfg) for k in wanted if (n.get(k) or c.get(k))]
    ordered = _order(diffs, chips, cfg)
    price = next((d for d in diffs if d.key == "price"), None)
    return Card(
        navy_id=navy_id, comp_id=comp_id,
        stronger=[d for d in ordered if d.direction == NAVY_BETTER],
        watch=[d for d in ordered if d.direction == COMP_BETTER],
        trade_offs=[d for d in ordered if d.direction in (TRADE_OFF, DIFFERENT)],
        same=[d for d in ordered if d.direction == SAME],
        missing=[d for d in ordered if d.direction == MISSING],
        price=price,
    )


@dataclass
class Row:
    key: str
    label: str
    group: str
    cells: list[Optional[Value]]
    best: set[int]          # column indexes holding the best value
    same: bool
    tags: set[str]


def matrix(columns: list[int], vals: dict[int, dict[str, Value]], *, keys: Optional[list[str]] = None,
           show_all: bool = False, cfg: Optional[dict] = None) -> list[Row]:
    """Rows of the side-by-side table. With `keys`, exactly those benefits; otherwise the core rows and
    every other benefit on which the policies differ (or every benefit, with show_all)."""
    present = set().union(*(vals.get(c, {}).keys() for c in columns)) if columns else set()
    if keys:
        wanted = [k for k in keys if k in present or k in FEATURES]
    else:
        wanted = sorted(present, key=lambda k: (GROUPS.index(FEATURES[k].group) if k in FEATURES else 99,
                                                list(FEATURES).index(k) if k in FEATURES else 0))
    rows: list[Row] = []
    for k in wanted:
        cells = [vals.get(c, {}).get(k) for c in columns]
        if not any(cells):
            continue
        shown = [(v.display, v.note) if v else None for v in cells]
        same = len(set(shown)) == 1
        if not keys and not show_all and same and k not in CORE_ROWS:
            continue
        best: set[int] = set()
        kind = next(v.kind for v in cells if v is not None)
        ranked = [(i, v) for i, v in enumerate(cells) if v is not None and v.rank is not None]
        if len(ranked) >= 2 and not same and kind != "excess":
            bases = {v.basis for _, v in ranked if v.basis != "any"}
            if len(bases) <= 1:
                top = max(v.rank for _, v in ranked)
                best = {i for i, v in ranked if v.rank == top}
                if len(best) == len(ranked):
                    best = set()
        f = FEATURES.get(k)
        rows.append(Row(key=k, label=f.label if f else k, group=f.group if f else "", cells=cells, best=best,
                        same=same, tags=mat.tags_for(k, cfg)))
    return rows
