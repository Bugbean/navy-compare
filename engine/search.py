"""Finding policies from how a rep describes them.

A rep says "the gold one with dental" or "HCF bronze", not the policy's exact name. Each word the
rep gives must appear somewhere in the policy's name, its fund, or the brand it is sold under. That
is deliberately strict: a loose match would put the wrong product on a button. Results are ranked
by how closely the name matches the words, and capped so the list stays usable.
"""
from __future__ import annotations

import re
from typing import Optional

from engine.db import Snapshot

MAX_RESULTS = 40

_SEARCH = """
SELECT p.id, p.current_name, p.product_type, p.is_navy, p.status,
       f.name AS fund_name, p.fund_code,
       coalesce(string_agg(DISTINCT b.name, ' | '), '') AS brands
FROM policies p
JOIN funds f ON f.fund_code = p.fund_code
LEFT JOIN brands b ON b.fund_code = p.fund_code
WHERE p.status <> 'closed'
  AND (:navy_all OR p.is_navy = :navy)
  AND (:product_type = '' OR p.product_type = :product_type)
  AND (:fund = '' OR p.fund_code = :fund)
GROUP BY p.id, f.name
ORDER BY p.is_navy DESC, p.current_name
"""

_WORD = re.compile(r"[a-z0-9+$']+")


def words(text: str) -> list[str]:
    """The searchable words of a phrase, lower case, with punctuation dropped. '$750' and 'gold' both count."""
    return [w for w in _WORD.findall((text or "").lower()) if len(w) > 1 or w.isdigit()]


def find(db: Snapshot, *, text: str = "", navy: Optional[bool] = None, product_type: str = "",
         fund: str = "") -> tuple[list[dict], int]:
    """Policies matching every word of `text` (or all of them when text is empty). Returns the first
    MAX_RESULTS and the total, so the page can say how many were left out."""
    terms = words(text)
    rows = db.rows(_SEARCH, {
        "navy_all": navy is None, "navy": bool(navy), "product_type": product_type or "", "fund": fund or ""})
    matches = []
    for r in rows:
        haystack = " ".join([r["current_name"], r["fund_name"], r["fund_code"], r["brands"]]).lower()
        if all(t in haystack for t in terms):
            name = r["current_name"].lower()
            score = sum(3 for t in terms if t in name) + sum(1 for t in terms if t not in name)
            matches.append((-score, r))
    matches.sort(key=lambda m: (m[0], m[1]["current_name"]))
    results = [r for _, r in matches]
    return results[:MAX_RESULTS], len(results)


def feature_match(db: Snapshot, *, categories: list[str], state: str, navy: Optional[bool]) -> list[dict]:
    """Policies that cover every one of these clinical categories in full in this state. Each category is
    read through hospital_cover_for, so the state rule is the same one the comparison uses. A restricted
    category is not counted as covered."""
    from config.reference import CLINICAL_CATEGORIES
    from engine.db import EngineError, lit
    if state not in ("NSW", "VIC", "QLD", "SA", "WA", "TAS", "NT", "ACT"):
        raise EngineError(f"unknown state {state!r}")
    if not categories:
        return []
    bad = [c for c in categories if c not in CLINICAL_CATEGORIES]
    if bad:
        raise EngineError(f"unknown clinical categories {bad}")
    wanted = "ARRAY[" + ", ".join(lit(c) for c in sorted(set(categories))) + "]"
    sql = f"""
    SELECT p.id, p.current_name, p.product_type, p.is_navy, f.name AS fund_name
    FROM policies p JOIN funds f ON f.fund_code = p.fund_code
    WHERE p.status <> 'closed' AND (:navy_all OR p.is_navy = :navy)
      AND NOT EXISTS (
          SELECT 1 FROM unnest({wanted}) AS cat
          WHERE NOT EXISTS (
              SELECT 1 FROM hospital_cover_for(p.id, :snap, :state) h
              WHERE h.category = cat AND h.cover = 'Covered'))
    ORDER BY p.is_navy DESC, p.current_name
    """
    return db.rows(sql, {"navy_all": navy is None, "navy": bool(navy), "snap": db.live_snapshot_id(),
                         "state": state})[:MAX_RESULTS]


def extras_match(db: Snapshot, *, services: list[str], state: str, family: str, navy: Optional[bool]) -> list[dict]:
    """Policies that cover every one of these extras services for this household in this state."""
    from config.reference import EXTRAS_SERVICES
    from engine.db import EngineError, lit
    if not services:
        return []
    bad = [x for x in services if x not in EXTRAS_SERVICES]
    if bad:
        raise EngineError(f"unknown extras services {bad}")
    wanted = "ARRAY[" + ", ".join(lit(x) for x in sorted(set(services))) + "]"
    sql = f"""
    SELECT p.id, p.current_name, p.product_type, p.is_navy, f.name AS fund_name
    FROM policies p JOIN funds f ON f.fund_code = p.fund_code
    WHERE p.status <> 'closed' AND (:navy_all OR p.is_navy = :navy)
      AND NOT EXISTS (
          SELECT 1 FROM unnest({wanted}) AS svc
          WHERE NOT EXISTS (
              SELECT 1 FROM extras_services_for(p.id, :snap, :state, :family) e
              WHERE e.service = svc AND e.covered))
    ORDER BY p.is_navy DESC, p.current_name
    """
    return db.rows(sql, {"navy_all": navy is None, "navy": bool(navy), "snap": db.live_snapshot_id(),
                         "state": state, "family": family})[:MAX_RESULTS]
