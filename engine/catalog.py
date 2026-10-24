"""The type-ahead index: every live policy and fund, searchable as a rep types.

A rep rarely knows a policy's full name. They know the company ("HCF", "Bupa", "Frank") and a word or
two of the product ("bronze", "gold 750"). Each word typed must be the start of a word in the policy's
name, its fund's name or code, or a brand the fund sells under, so "hcf bro" finds HCF's Bronze
policies and "frank" finds GMHBA's Frank range. When no policy matches every word, filler and words
no policy has are set aside (and reported), so "defence health top cover" still finds Defence Health's
Top cover. Companies are suggested as well as policies, so the rep can pick the company first and then
choose from its products.

The index is held in memory and rebuilt from the database every few minutes (the data changes once a
month), so a keystroke never waits on the database.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

from engine.db import Snapshot

REFRESH_SECONDS = 300
TIER_LABELS = {"Basic": "Basic", "BasicPlus": "Basic Plus", "Bronze": "Bronze", "BronzePlus": "Bronze Plus",
               "Silver": "Silver", "SilverPlus": "Silver Plus", "Gold": "Gold"}

_LOAD = """
WITH ex AS (
    SELECT policy_id, array_agg(DISTINCT excess_option ORDER BY excess_option) AS excesses
    FROM variants WHERE snapshot_id = :snap AND status = 'Open' GROUP BY policy_id)
SELECT p.id, p.current_name, p.product_type, p.is_navy, p.is_corporate, p.fund_code, f.name AS fund_name,
       coalesce(ex.excesses, ARRAY[]::numeric[]) AS excesses,
       f.fund_type, coalesce(br.name, '') AS brand_name,
       coalesce((SELECT string_agg(b.name, ' | ') FROM brands b WHERE b.fund_code = p.fund_code), '') AS fund_brands,
       (SELECT hp.hospital_tier FROM hospital_profiles hp
         WHERE hp.policy_id = p.id AND hp.snapshot_id = :snap
         ORDER BY (hp.state = 'ALL') DESC, hp.state LIMIT 1) AS tier
FROM policies p
JOIN funds f ON f.fund_code = p.fund_code
JOIN ex ON ex.policy_id = p.id
LEFT JOIN brands br ON br.brand_code = p.brand_code
WHERE p.status <> 'closed'
ORDER BY p.current_name
"""

_WORD = re.compile(r"[a-z0-9]+")


def words(text: str) -> list[str]:
    """Lower-case words, with '+' read as 'plus' and '&' as 'and', so 'Silver+' and 'SILVER PLUS' match."""
    t = (text or "").lower().replace("+", " plus ").replace("&", " and ")
    return _WORD.findall(t)


@dataclass(frozen=True)
class PolicyEntry:
    id: int
    name: str
    product_type: str
    is_navy: bool
    fund_code: str
    fund_name: str
    restricted: bool
    brand: str
    tier: Optional[str]
    name_words: tuple[str, ...]
    other_words: tuple[str, ...]
    corporate: bool = False
    excesses: tuple[float, ...] = ()

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "type": self.product_type, "is_navy": self.is_navy,
                "fund_code": self.fund_code, "fund_name": self.fund_name, "brand": self.brand,
                "tier": TIER_LABELS.get(self.tier or "", ""), "restricted": self.restricted,
                "corporate": self.corporate, "excesses": list(self.excesses)}


@dataclass(frozen=True)
class FundEntry:
    code: str
    name: str
    brands: tuple[str, ...]
    restricted: bool
    is_navy: bool
    count: int
    words: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"code": self.code, "name": self.name, "brands": list(self.brands), "restricted": self.restricted,
                "count": self.count}


class Catalog:
    def __init__(self, policies: list[PolicyEntry], snapshot_id: int):
        self.policies = policies
        self.snapshot_id = snapshot_id
        self.aliases: dict[str, list[str]] = {}
        by_fund: dict[str, list[PolicyEntry]] = {}
        for p in policies:
            by_fund.setdefault(p.fund_code, []).append(p)
        self.funds = []
        for code, ps in sorted(by_fund.items()):
            first = ps[0]
            brands = tuple(sorted({p.brand for p in ps if p.brand}))
            fw = set(words(first.fund_name)) | {code.lower()} | {w for b in brands for w in words(b)}
            self.funds.append(FundEntry(code, first.fund_name, brands, first.restricted, first.is_navy, len(ps),
                                        tuple(sorted(fw))))

    @classmethod
    def from_rows(cls, rows: list[dict], snapshot_id: int,
                  aliases: Optional[dict[str, list[str]]] = None) -> "Catalog":
        aliases = aliases or {}
        entries = []
        for r in rows:
            other = set(words(r["fund_name"])) | {r["fund_code"].lower()} | set(words(r["brand_name"])) \
                | set(words(r["fund_brands"])) | {w for a in aliases.get(r["fund_code"], []) for w in words(a)} \
                | {str(int(float(x))) for x in (r.get("excesses") or []) if float(x) > 0}
            entries.append(PolicyEntry(
                id=int(r["id"]), name=r["current_name"], product_type=r["product_type"], is_navy=bool(r["is_navy"]),
                fund_code=r["fund_code"], fund_name=r["fund_name"], restricted=r["fund_type"] == "Restricted",
                brand=r["brand_name"], tier=r["tier"], name_words=tuple(words(r["current_name"])),
                other_words=tuple(sorted(other)), corporate=bool(r.get("is_corporate")),
                excesses=tuple(float(x) for x in (r.get("excesses") or []))))
        built = cls(entries, snapshot_id)
        built.aliases = aliases
        return built

    def search(self, query: str, *, side: str = "any", fund: str = "", product_type: str = "",
               limit: int = 8) -> dict:
        """Up to `limit` policies and the matching companies. `side` is navy, comp or any; `fund` keeps
        to one company's policies once the rep has picked it.

        Every word must match. When nothing matches them all, filler words and words no policy here
        has are dropped, then at most two more (those matching fewest), as the browser does; the words
        dropped are returned, so the rep can see what was not used."""
        terms = words(query)
        pols = [p for p in self.policies
                if (side == "any" or p.is_navy == (side == "navy"))
                and (not fund or p.fund_code == fund)
                and (not product_type or p.product_type == product_type)]
        dropped: list[str] = []
        scored = _rank(pols, terms)
        if not scored and terms:
            from config.language import STOP_WORDS
            keep = [t for t in terms if t not in STOP_WORDS and any(_has(p, t) for p in pols)]
            dropped = [t for t in terms if t not in keep]
            terms = keep
            scored = _rank(pols, terms) if terms else []
            for _ in range(2):
                if scored or len(terms) < 2:
                    break
                counts = [sum(_has(p, t) for p in pols) for t in terms]
                dropped.append(terms.pop(counts.index(min(counts))))
                scored = _rank(pols, terms)
        policies = [s[3] for s in scored]

        funds = []
        if terms and not fund and side != "navy":
            for f in self.funds:
                if side == "comp" and f.is_navy:
                    continue
                if all(any(w.startswith(t) for w in f.words) for t in terms):
                    funds.append(f)
            funds.sort(key=lambda f: (not f.code.lower().startswith(terms[0]), f.name))
        return {"policies": [p.as_dict() for p in policies[:limit]], "total": len(policies),
                "funds": [f.as_dict() for f in funds[:5]], "dropped": dropped}

    def get(self, policy_id: int) -> Optional[PolicyEntry]:
        return next((p for p in self.policies if p.id == policy_id), None)


def _has(p: PolicyEntry, term: str) -> bool:
    return any(w.startswith(term) for w in p.name_words) or any(w.startswith(term) for w in p.other_words)


def _rank(pols: list[PolicyEntry], terms: list[str]) -> list[tuple]:
    """Policies matching every term, best first: a word of the name scores more than a word of the
    company, and a name that starts with the words as typed more again."""
    scored = []
    for p in pols:
        total = 0
        for t in terms:
            if any(w.startswith(t) for w in p.name_words):
                total += 5
            elif any(w.startswith(t) for w in p.other_words):
                total += 2
            else:
                break
        else:
            if terms and " ".join(p.name_words).startswith(" ".join(terms)):
                total += 6
            scored.append((-total, p.product_type != "hospital", p.name, p))
    scored.sort(key=lambda s: s[:3])
    return scored


_lock = threading.Lock()
_cache: dict[str, tuple[float, Catalog]] = {}


def catalog(db: Snapshot) -> Catalog:
    """The index for this database. Every REFRESH_SECONDS it checks which snapshot is live, and it is
    rebuilt only when that has changed, so a rep never waits on a rebuild of the same data."""
    key = db.url
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < REFRESH_SECONDS:
            return hit[1]
    from config.language import fund_aliases
    snap = db.live_snapshot_id()
    if hit and hit[1].snapshot_id == snap:
        with _lock:
            _cache[key] = (time.monotonic(), hit[1])
        return hit[1]
    built = Catalog.from_rows(db.rows(_LOAD, {"snap": snap}), snap, aliases=fund_aliases())
    with _lock:
        _cache[key] = (time.monotonic(), built)
    return built


def clear() -> None:
    with _lock:
        _cache.clear()
