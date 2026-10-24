"""What the rep might want next, worked out from the comparison itself rather than asked of a model.

  cheaper_navy   Navy Health policies of the same kind that cost this household less than the one on the
                 card, closest in cover to the competitor first: the answer to "they're cheaper".
  follow_ups     One-tap next questions drawn from the card: a member need the card has something to say
                 about, a cheaper Navy Health option, another competitor, the full table.
  verdict        The one-line summary a rep reads first.

Each is deterministic, so it costs nothing, shows instantly, and never says anything the card does not.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from engine import materiality as mat
from engine import queries as q
from engine.db import Snapshot
from engine.differences import COMP_BETTER, NAVY_BETTER, Card

_CHEAPER = """
WITH cur AS (
    SELECT min(premium_month) AS premium FROM variants_for(:navy, :snap, :state)
    WHERE status = 'Open' AND family_type = :family AND dependant_scope = :scope),
cand AS (
    SELECT p.id, p.current_name, min(v.premium_month) AS premium
    FROM policies p CROSS JOIN LATERAL variants_for(p.id, :snap, :state) v
    WHERE p.is_navy AND p.status <> 'closed' AND p.product_type = :ptype AND p.id <> :navy
      AND v.status = 'Open' AND v.family_type = :family AND v.dependant_scope = :scope
    GROUP BY p.id, p.current_name)
SELECT c.id, c.current_name, c.premium,
       (SELECT pc.score FROM policy_comparisons pc
         WHERE pc.snapshot_id = :snap AND pc.navy_policy_id = c.id AND pc.comp_policy_id = :comp
           AND pc.state IN (:state, 'ALL') AND pc.family_type IN (:family, 'ALL')
         ORDER BY (pc.state = :state) DESC, (pc.family_type = :family) DESC LIMIT 1) AS score
FROM cand c, cur
WHERE c.premium < cur.premium
ORDER BY score DESC NULLS LAST, c.premium
LIMIT 3
"""


CHEAPER_CACHE_SECONDS = 300
_cheaper_cache: dict[tuple, tuple[float, list[dict]]] = {}
_cheaper_lock = threading.Lock()


def cheaper_navy(db: Snapshot, *, navy_id: int, comp_id: int, product_type: str, state: str, family: str,
                 scope: str) -> list[dict]:
    """Up to three Navy Health policies that cost this household less than `navy_id`, the closest in
    cover to the competitor first. Kept for a few minutes per snapshot, since a call asks again and
    again about the same pair."""
    state, family, scope = q._state(state), q._family(family), q._scope(scope)
    if product_type not in ("hospital", "extras", "combined"):
        return []
    snap = db.live_snapshot_id()
    key = (db.url, snap, navy_id, comp_id, product_type, state, family, scope)
    now = time.monotonic()
    with _cheaper_lock:
        hit = _cheaper_cache.get(key)
        if hit and now - hit[0] < CHEAPER_CACHE_SECONDS:
            return [dict(r) for r in hit[1]]
    rows = db.rows(_CHEAPER, {"navy": navy_id, "comp": comp_id, "snap": snap, "state": state,
                              "family": family, "scope": scope, "ptype": product_type})
    with _cheaper_lock:
        if len(_cheaper_cache) > 2000:
            _cheaper_cache.clear()
        _cheaper_cache[key] = (time.monotonic(), rows)
    return [dict(r) for r in rows]


def _money(v: float) -> str:
    return f"${v:,.2f}"


def verdict(card: Card, navy_name: str, comp_name: str) -> str:
    """The card in one line: how many benefits each side wins, and the premium gap. The premium has its
    own clause, so it is not counted as a benefit too."""
    n = len([d for d in card.stronger if d.key != "price"])
    c = len([d for d in card.watch if d.key != "price"])
    parts = []
    if n or c:
        parts.append(f"Navy Health is better on {n} benefit{'s' if n != 1 else ''}"
                     f" and {comp_name} on {c}")
    p = card.price
    if p and p.navy and p.comp and p.navy.size is not None and p.comp.size is not None:
        gap = abs(p.navy.size - p.comp.size)
        same_excess = p.navy.note == p.comp.note
        if gap < 0.005:
            parts.append("the same premium")
        else:
            who = "Navy Health" if p.navy.size < p.comp.size else comp_name
            parts.append(f"{who} is {_money(gap)} a month cheaper"
                         + ("" if same_excess else " (at a different excess)"))
    if not parts:
        return f"{navy_name} and {comp_name} are the same on the benefits compared."
    return "; ".join(parts) + "."


def follow_ups(card: Card, *, active_chips: set[str], cheaper: list[dict], focus: list[str]) -> list[dict]:
    """Up to five next steps, each a button: {kind, label, ...}."""
    out: list[dict] = []
    if cheaper and card.price and card.price.direction == COMP_BETTER:
        best = cheaper[0]
        out.append({"kind": "swap_navy", "id": best["id"], "name": best["current_name"],
                    "label": f"Cheaper Navy option: {best['current_name']} ({_money(float(best['premium']))} a month)"})
    labels = {c["key"]: c["label"] for c in mat.chips()}
    seen: set[str] = set()
    for d in card.stronger + card.watch:
        for tag in sorted(d.tags):
            if tag in active_chips or tag in seen:
                continue
            seen.add(tag)
            side = "Navy Health is better" if d.direction == NAVY_BETTER else "theirs is better"
            out.append({"kind": "need", "chip": tag, "label": f"{labels.get(tag, tag)}? ({side} on {d.label.lower()})"})
            break
        if len([o for o in out if o["kind"] == "need"]) >= 2:
            break
    if focus:
        out.append({"kind": "clear_focus", "label": "Show every difference again"})
    out.append({"kind": "add_comp", "label": "Add another competitor"})
    out.append({"kind": "table", "label": "Side by side table"})
    return out[:5]


def closest_alternatives(closest: list[dict], comp_id: int, exclude: Optional[set[int]] = None) -> list[dict]:
    exclude = exclude or set()
    return [{"id": m["navy_id"], "name": m["navy_name"], "score": float(m["score"])}
            for m in closest if m["comp_id"] == comp_id and m["navy_id"] not in exclude]
