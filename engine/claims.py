"""The call sheet: what a member can actually claim on each policy, laid out the way a rep explains it.

The comparison card (engine/differences.py) ranks every benefit flat by a general materiality score,
which puts a difference in orthoptics ahead of the dental limit nearly every member uses. A rep on the
phone needs the opposite: every extras service with its annual limit, what a typical visit pays back
and any wait, in the order members actually claim, with the gap between the two policies said in
dollars. And for hospital, which conditions one policy covers that the other does not, plus the few
things that apply to every admission.

Nothing here is a new figure: every Value comes from engine/benefits.py and every comparison from
engine/differences.compare_pair. This only chooses the order and says the gap plainly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from config.features import FEATURES, SERVICE_LABELS, item_service
from config.reference import EXTRAS_ITEMS, EXTRAS_SERVICES
from engine.benefits import UNLIMITED, Value
from engine.differences import COMP_BETTER, DIFFERENT, NAVY_BETTER, SAME, TRADE_OFF, Difference, compare_pair

# The order members claim in, most common first: the services a rep is asked about on nearly every
# call lead, the specialist ones follow. Every service is listed once.
SERVICE_ORDER = (
    "DentalGeneral", "Optical", "Physiotherapy", "DentalMajor", "Endodontic", "Chiropractic",
    "RemedialMassage", "Osteopathy", "Podiatry", "Psychology", "Orthodontic", "HearingAids",
    "Acupuncture", "Dietetics", "ExercisePhysiology", "Orthotics", "NonPBS", "Audiology",
    "SpeechTherapy", "OccupationalTherapy", "ChineseHerbalMedicine", "Vaccinations",
    "HealthManagement", "GlucoseMonitor", "Orthoptics", "AntenatalPostnatal", "HomeNursing",
)
assert sorted(SERVICE_ORDER) == sorted(EXTRAS_SERVICES)
EVERYDAY_SERVICES = SERVICE_ORDER[:6]

# Every item: key that belongs to each service, in the reference order.
_ITEMS_BY_SERVICE: dict[str, list[str]] = {}
for _item, (_svc, _label) in EXTRAS_ITEMS.items():
    _ITEMS_BY_SERVICE.setdefault(_svc, []).append(_item)

ITEMS_IN_CELL = 2


def _money(v: float) -> str:
    return f"${v:,.2f}" if v != int(v) else f"${v:,.0f}"


def short_item(item: str) -> str:
    """"physiotherapy, first visit" -> "first visit"; "crown (item 615)" -> "crown"."""
    label = EXTRAS_ITEMS[item][1].split(" (item")[0]
    head, _, tail = label.partition(", ")
    return tail if tail in ("first visit", "follow-up visit") else label


def services_in(keys: Iterable[str]) -> list[str]:
    """The extras services these feature keys touch (via their extras:, limit: or item: key), in the
    order first mentioned."""
    out: list[str] = []
    for k in keys:
        svc = item_service(k)
        if svc and svc not in out:
            out.append(svc)
    return out


def categories_in(keys: Iterable[str]) -> list[str]:
    """The clinical categories (cover: keys) these feature keys name, in the order first mentioned."""
    out: list[str] = []
    for k in keys:
        if k.startswith("cover:") and k not in out:
            out.append(k)
    return out


# ── extras ────────────────────────────────────────────────────────────────────

@dataclass
class ExtrasCell:
    """One policy's side of one service, as the rep reads it."""
    covered: Optional[bool]          # None: the policy has no extras at all
    limit: str = ""                  # "$800 a person a year"
    shared: str = ""                 # "Glucose monitor, Hearing aids" when the limit is shared
    wait: str = ""                   # "2 month wait"
    items: list[tuple[str, str]] = field(default_factory=list)   # (short label, what it pays)


@dataclass
class ExtrasRow:
    service: str
    label: str
    navy: ExtrasCell
    comp: ExtrasCell
    direction: str                   # navy_better, comp_better, same, trade_off, different
    gap: str                         # the difference said plainly: "$500 more a year", "Navy Health only"
    items: list[Difference]          # every billed item, for the expanded detail
    limit: Optional[Difference]
    focus: bool = False


def _cell(svc: str, side: dict[str, Value]) -> ExtrasCell:
    cov = side.get(f"extras:{svc}")
    if cov is None:
        return ExtrasCell(covered=None)
    if not cov.rank:
        return ExtrasCell(covered=False)
    cell = ExtrasCell(covered=True, wait=f"{cov.wait:g} month wait" if cov.wait else "")
    lim = side.get(f"limit:{svc}")
    if lim is not None:
        main, _, shared = lim.display.partition(" (shared with ")
        cell.limit, cell.shared = main, shared.rstrip(")")
    else:
        cell.limit = "No limit listed"
    for item in _ITEMS_BY_SERVICE.get(svc, []):
        v = side.get(f"item:{item}")
        if v is not None and v.display != "Not covered" and len(cell.items) < ITEMS_IN_CELL:
            cell.items.append((short_item(item), v.display))
    return cell


def _who(direction: str) -> str:
    return "Navy Health" if direction == NAVY_BETTER else "Theirs"


def _gap(coverage: Optional[Difference], limit: Optional[Difference],
         items: list[Difference]) -> tuple[str, str]:
    """(direction, the gap in plain words), judged in the order a member feels it: whether it is
    covered at all, then the yearly cap, then what each visit pays back."""
    if coverage and coverage.direction in (NAVY_BETTER, COMP_BETTER):
        return coverage.direction, f"{_who(coverage.direction)} only"
    if limit and limit.direction in (NAVY_BETTER, COMP_BETTER):
        side = limit.navy if limit.direction == NAVY_BETTER else limit.comp
        if limit.size == UNLIMITED:
            return limit.direction, f"{_who(limit.direction)}: no annual limit"
        if limit.size and side and side.basis in ("per_person", "per_policy", "per_service", "lifetime"):
            return limit.direction, f"{_who(limit.direction)}: {_money(limit.size)} more a year"
        return limit.direction, f"{_who(limit.direction)}: higher limit"
    better = [d for d in items if d.direction in (NAVY_BETTER, COMP_BETTER)]
    if better:
        directions = {d.direction for d in better}
        if len(directions) > 1:
            return TRADE_OFF, "Each pays more on some items"
        top = max(better, key=lambda d: d.size or 0)
        item = top.key.split(":", 1)[1]
        bases = {v.basis for v in (top.navy, top.comp) if v is not None}
        if top.size and "dollars" in bases and "percent" not in bases:
            return top.direction, f"{_who(top.direction)}: {_money(top.size)} more per {short_item(item)}"
        return top.direction, f"{_who(top.direction)}: pays more per visit"
    if limit and limit.direction == DIFFERENT:
        return DIFFERENT, "Limits work differently"
    return SAME, "Same"


def extras_sheet(navy_id: int, comp_id: int, vals: dict[int, dict[str, Value]], *,
                 focus: Iterable[str] = (), cfg: Optional[dict] = None) -> tuple[list[ExtrasRow], list[str]]:
    """Every extras service either policy covers, in the order members claim (services the rep asked
    about first), and the labels of those neither covers. Empty when neither policy has extras."""
    n, c = vals.get(navy_id, {}), vals.get(comp_id, {})
    focus = [s for s in focus if s in SERVICE_ORDER]
    order = focus + [s for s in SERVICE_ORDER if s not in focus]
    rows: list[ExtrasRow] = []
    neither: list[str] = []
    for svc in order:
        cov_key, lim_key = f"extras:{svc}", f"limit:{svc}"
        nv, cv = n.get(cov_key), c.get(cov_key)
        if nv is None and cv is None:
            continue
        label = SERVICE_LABELS.get(svc, svc)
        if not (nv and nv.rank) and not (cv and cv.rank):
            neither.append(label)
            continue
        coverage = compare_pair(cov_key, nv, cv, cfg)
        limit = compare_pair(lim_key, n.get(lim_key), c.get(lim_key), cfg) if (n.get(lim_key) or c.get(lim_key)) else None
        items = [compare_pair(f"item:{i}", n.get(f"item:{i}"), c.get(f"item:{i}"), cfg)
                 for i in _ITEMS_BY_SERVICE.get(svc, []) if n.get(f"item:{i}") or c.get(f"item:{i}")]
        direction, gap = _gap(coverage, limit, items)
        rows.append(ExtrasRow(service=svc, label=label, navy=_cell(svc, n), comp=_cell(svc, c),
                              direction=direction, gap=gap, items=items, limit=limit, focus=svc in focus))
    return rows, neither


# ── hospital ──────────────────────────────────────────────────────────────────

# What applies to every admission, whatever the condition. A clinical category carries no dollar
# benefit or wait of its own (docs/DATA_MODEL.md), so these are what "covered" costs and waits on.
HOSPITAL_ESSENTIALS = ("tier", "accommodation", "excess", "copayment", "known_gap", "wait:PreExisting",
                       "wait:Other", "wait:PregnancyBirth", "ambulance")


@dataclass
class HospitalSheet:
    navy_only: list[Difference]      # covered (or covered more fully) by Navy Health only
    comp_only: list[Difference]      # covered (or covered more fully) by theirs only
    both: int                        # clinical categories both cover the same way
    essentials: list[Difference]


def hospital_sheet(navy_id: int, comp_id: int, vals: dict[int, dict[str, Value]], *,
                   cfg: Optional[dict] = None) -> Optional[HospitalSheet]:
    """The hospital side of the call sheet, or None when neither policy has hospital cover."""
    n, c = vals.get(navy_id, {}), vals.get(comp_id, {})
    cats = sorted({k for k in (*n, *c) if k.startswith("cover:")},
                  key=lambda k: FEATURES[k].label if k in FEATURES else k)
    if not cats:
        return None
    diffs = [compare_pair(k, n.get(k), c.get(k), cfg) for k in cats]
    return HospitalSheet(
        navy_only=[d for d in diffs if d.direction == NAVY_BETTER],
        comp_only=[d for d in diffs if d.direction == COMP_BETTER],
        both=sum(1 for d in diffs if d.direction == SAME and d.navy and d.navy.rank),
        essentials=[compare_pair(k, n.get(k), c.get(k), cfg) for k in HOSPITAL_ESSENTIALS if n.get(k) or c.get(k)],
    )


# ── price ─────────────────────────────────────────────────────────────────────

@dataclass
class PriceGap:
    cheaper: str                     # navy_better, comp_better or same
    month: float
    year: float


def price_gap(price: Optional[Difference]) -> Optional[PriceGap]:
    """How much cheaper one policy is, a month and a year, at the excess the card compares."""
    if price is None or price.navy is None or price.comp is None:
        return None
    month = round(abs(price.navy.size - price.comp.size), 2)
    cheaper = SAME if month == 0 else (NAVY_BETTER if price.navy.size < price.comp.size else COMP_BETTER)
    return PriceGap(cheaper=cheaper, month=month, year=round(month * 12, 2))
