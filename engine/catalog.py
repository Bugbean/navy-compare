"""The type-ahead index: every live policy and fund, searchable as a rep types.

A rep rarely knows a policy's full name. They know the company ("HCF", "Bupa", "Frank") and a word or
two of the product ("bronze", "gold 750"). Each word typed must be the start of a word in the policy's
name, its fund's name or code, or a brand the fund sells under, so "hcf bro" finds HCF's Bronze
policies and "frank" finds GMHBA's Frank range. Companies are suggested as well as policies, so the rep
can pick the company first and then choose from its products.

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
SELECT p.id, p.current_name, p.product_type, p.is_navy, p.fund_code, f.name AS fund_name,
       f.fund_type, coalesce(br.name, '') AS brand_name,
       coalesce((SELECT string_agg(b.name, ' | ') FROM brands b WHERE b.fund_code = p.fund_code), '') AS fund_brands,
       (SELECT hp.hospital_tier FROM hospital_profiles hp
         WHERE hp.policy_id = p.id AND hp.snapshot_id = :snap
         ORDER BY (hp.state = 'ALL') DESC, hp.state LIMIT 1) AS tier
FROM policies p
JOIN funds f ON f.fund_code = p.fund_code
LEFT JOIN brands br ON br.brand_code = p.brand_code
WHERE p.status <> 'closed'
  AND p.id IN (SELECT policy_id FROM variants WHERE snapshot_id = :snap AND status = 'Open')
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

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "type": self.product_type, "is_navy": self.is_navy,
                "fund_code": self.fund_code, "fund_name": self.fund_name, "brand": self.brand,
                "tier": TIER_LABELS.get(self.tier or "", ""), "restricted": self.restricted}


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
    def from_rows(cls, rows: list[dict], snapshot_id: int) -> "Catalog":
        entries = []
        for r in rows:
            other = set(words(r["fund_name"])) | {r["fund_code"].lower()} | set(words(r["brand_name"])) \
                | set(words(r["fund_brands"]))
            entries.append(PolicyEntry(
                id=int(r["id"]), name=r["current_name"], product_type=r["product_type"], is_navy=bool(r["is_navy"]),
                fund_code=r["fund_code"], fund_name=r["fund_name"], restricted=r["fund_type"] == "Restricted",
                brand=r["brand_name"], tier=r["tier"], name_words=tuple(words(r["current_name"])),
                other_words=tuple(sorted(other))))
        return cls(entries, snapshot_id)

    def search(self, query: str, *, side: str = "any", fund: str = "", product_type: str = "",
               limit: int = 8) -> dict:
        """Up to `limit` policies and the matching companies. `side` is navy, comp or any; `fund` keeps
        to one company's policies once the rep has picked it."""
        terms = words(query)
        pols = [p for p in self.policies
                if (side == "any" or p.is_navy == (side == "navy"))
                and (not fund or p.fund_code == fund)
                and (not product_type or p.product_type == product_type)]
        scored = []
        for p in pols:
            total = 0
            ok = True
            for t in terms:
                if any(w.startswith(t) for w in p.name_words):
                    total += 5
                elif any(w.startswith(t) for w in p.other_words):
                    total += 2
                else:
                    ok = False
                    break
            if not ok:
                continue
            if terms and " ".join(p.name_words).startswith(" ".join(terms)):
                total += 6
            scored.append((-total, p.product_type != "hospital", p.name, p))
        scored.sort(key=lambda s: s[:3])
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
                "funds": [f.as_dict() for f in funds[:5]]}

    def get(self, policy_id: int) -> Optional[PolicyEntry]:
        return next((p for p in self.policies if p.id == policy_id), None)


_lock = threading.Lock()
_cache: dict[str, tuple[float, Catalog]] = {}


def catalog(db: Snapshot) -> Catalog:
    """The index for this database, rebuilt when it is older than REFRESH_SECONDS."""
    key = db.url
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < REFRESH_SECONDS:
            return hit[1]
    snap = db.live_snapshot_id()
    built = Catalog.from_rows(db.rows(_LOAD, {"snap": snap}), snap)
    with _lock:
        _cache[key] = (time.monotonic(), built)
    return built


def clear() -> None:
    with _lock:
        _cache.clear()
