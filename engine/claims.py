"""The specific claim detail a rep can read aloud for one extras service.

The comparison card (engine/differences.py) ranks every benefit flat, so the exact dollar figures a
member actually asks about ("what do I get back for a scale and clean?") can end up several rows away
from the annual limit that caps them, mixed in with unrelated benefits. This groups both under the
service they belong to, in the order a rep would explain it to a member: is it covered, what does each
visit or item pay back, and what is the yearly cap on that.

Nothing here is a new figure: every Value comes from engine/benefits.py and every comparison from
engine/differences.compare_pair, so a block says exactly what the card already knows, just organised
the way a sales conversation actually goes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config.features import FEATURES, item_service
from config.reference import EXTRAS_ITEMS
from engine.benefits import Value
from engine.differences import Difference, compare_pair

MAX_ITEMS_SHOWN = 4

# Every item: key that belongs to each service, built once.
_ITEMS_BY_SERVICE: dict[str, list[str]] = {}
for _item, (_svc, _label) in EXTRAS_ITEMS.items():
    _ITEMS_BY_SERVICE.setdefault(_svc, []).append(_item)


@dataclass
class ServiceClaim:
    service: str
    label: str
    coverage: Optional[Difference]
    limit: Optional[Difference]
    items: list[Difference]
    more: int   # further billed items that exist for this service but are not shown


def services_in(keys: list[str]) -> list[str]:
    """The extras services these feature keys touch (via their extras:, limit: or item: key), in the
    order first mentioned. A key with no service (price, a clinical category, a waiting period) is
    simply not one of these; the rep still sees it in the plain direct answer."""
    out: list[str] = []
    for k in keys:
        svc = item_service(k)
        if svc and svc not in out:
            out.append(svc)
    return out


def categories_in(keys: list[str]) -> list[str]:
    """The clinical categories (cover: keys) these feature keys name, in the order first mentioned."""
    out: list[str] = []
    for k in keys:
        if k.startswith("cover:") and k not in out:
            out.append(k)
    return out


# What a rep needs to go with "is it covered": what it costs to be admitted, and how long a new
# member waits for it. A hospital clinical category never carries its own dollar benefit or its own
# wait — Australia's private health data has no such field (docs/DATA_MODEL.md, "Hospital waiting
# periods are 4 fixed buckets, never per category") — so this is the real, fund-wide context that
# answers "what does covered actually mean for her", not an invented per-condition figure.
HOSPITAL_CONTEXT_KEYS = ("excess", "copayment", "known_gap", "wait:PreExisting", "wait:Other")


def hospital_context(navy_id: int, comp_id: int, vals: dict[int, dict[str, Value]], *,
                     cfg: Optional[dict] = None) -> list[Difference]:
    """The excess, co-payment, known gap cover and waiting periods that apply to any hospital
    admission, shown once alongside whichever clinical categories were asked about rather than
    repeated per category, since none of them are specific to one condition."""
    n, c = vals.get(navy_id, {}), vals.get(comp_id, {})
    return [compare_pair(k, n.get(k), c.get(k), cfg) for k in HOSPITAL_CONTEXT_KEYS if n.get(k) or c.get(k)]


def claim_groups(navy_id: int, comp_id: int, vals: dict[int, dict[str, Value]], *,
                 services: list[str], cfg: Optional[dict] = None) -> list["ServiceClaim"]:
    """One block per service that either policy has data for: whether it's covered, the items billed
    under it ranked by how much the two policies differ (the biggest gap first), and the annual limit
    that caps them. A service neither policy has anything on is left out."""
    n, c = vals.get(navy_id, {}), vals.get(comp_id, {})
    out: list[ServiceClaim] = []
    for svc in services:
        cov_key, lim_key = f"extras:{svc}", f"limit:{svc}"
        coverage = (compare_pair(cov_key, n.get(cov_key), c.get(cov_key), cfg)
                    if (n.get(cov_key) or c.get(cov_key)) else None)
        limit = (compare_pair(lim_key, n.get(lim_key), c.get(lim_key), cfg)
                 if (n.get(lim_key) or c.get(lim_key)) else None)
        diffs = []
        for item in _ITEMS_BY_SERVICE.get(svc, []):
            key = f"item:{item}"
            nv, cv = n.get(key), c.get(key)
            if nv or cv:
                diffs.append(compare_pair(key, nv, cv, cfg))
        diffs.sort(key=lambda d: (-d.materiality, -(d.size or 0)))
        if not coverage and not limit and not diffs:
            continue
        label = FEATURES[cov_key].label if cov_key in FEATURES else svc
        out.append(ServiceClaim(service=svc, label=label, coverage=coverage, limit=limit,
                                items=diffs[:MAX_ITEMS_SHOWN], more=max(0, len(diffs) - MAX_ITEMS_SHOWN)))
    return out
