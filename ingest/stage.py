"""Read a release directory and write the staging CSVs.

Nothing is judged here beyond what the parser must know to read a record. The
gates a release has to pass are the ones this module can check against the
files themselves: each file's own Count, its Open or Closed status, and the
uniqueness of every ProductItemID. Business rules live in the promote SQL,
where the whole snapshot is visible at once.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Iterable

from ingest.feed import iter_elements, root_count, text
from ingest.funds import count_change_rows, read_agreement_hospitals, read_funds
from ingest.products import Registry, parse_product

PRODUCT_FILES = (
    "Hospital Open", "Hospital Closed",
    "GeneralHealth Open", "GeneralHealth Closed",
    "Combined Open", "Combined Closed",
)

PRODUCT_COLUMNS = ("gov_product_item_id", "gov_product_id", "gov_product_code", "fund_code",
                   "product_type", "status", "name", "state", "premium_month", "source_file", "raw")
FUND_COLUMNS = ("fund_code", "fund_item_id", "raw")
AGREEMENT_COLUMNS = ("row_no", "provider_code", "raw")
DEFINITION_COLUMNS = ("def_id", "kind", "body")
LIMIT_GROUP_COLUMNS = ("lg_id", "body")


class ReleaseError(ValueError):
    """The release does not pass a gate. Nothing is promoted."""


@dataclass
class Release:
    directory: str
    release_date: str
    file_names: dict[str, str] = field(default_factory=dict)  # logical name -> file name on disk
    counts: dict[str, int] = field(default_factory=dict)
    expected: dict[str, int] = field(default_factory=dict)
    change_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    sha256: str = ""


def _find(directory: str, prefix: str) -> str:
    matches = [f for f in os.listdir(directory) if f.startswith(prefix) and f.endswith(".xml")]
    if len(matches) != 1:
        raise ReleaseError(f"expected one file starting {prefix!r} in {directory}, found {matches}")
    return matches[0]


def _find_csv(directory: str, prefix: str) -> str:
    matches = [f for f in os.listdir(directory) if f.startswith(prefix) and f.endswith(".csv")]
    if len(matches) != 1:
        raise ReleaseError(f"expected one CSV starting {prefix!r} in {directory}, found {matches}")
    return matches[0]


def _manifest_hash(directory: str, names: Iterable[str]) -> str:
    """A hash over the content of every file in the release, in a fixed order,
    so a release can be recognised again if it is staged twice."""
    h = hashlib.sha256()
    for name in sorted(names):
        h.update(name.encode("utf-8"))
        with open(os.path.join(directory, name), "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def stage_release(directory: str, out_dir: str, release_date: str) -> Release:
    """Parse every file in the release into CSVs under out_dir and return the
    counts. Raises ReleaseError if a gate fails."""
    os.makedirs(out_dir, exist_ok=True)
    rel = Release(directory=directory, release_date=release_date)

    fund_file = _find(directory, "Funds ")
    agreement_name = _find_csv(directory, "Agreement Hospitals ")
    fund_change_name = _find_csv(directory, "Fund Changes ")
    product_change_name = _find_csv(directory, "Product Changes ")

    rel.file_names = {"funds": fund_file, "agreements": agreement_name}
    for logical in PRODUCT_FILES:
        rel.file_names[logical] = _find(directory, logical + " ")

    all_names = list(rel.file_names.values()) + [fund_change_name, product_change_name]
    rel.sha256 = _manifest_hash(directory, all_names)

    # Funds first: products inherit ambulance cover, known-gap cover and
    # preferred providers from their fund.
    fund_records, fund_defaults = read_funds(os.path.join(directory, fund_file))
    rel.counts["funds"] = len(fund_records)
    rel.expected["funds"] = root_count(os.path.join(directory, fund_file), "Funds")
    if rel.counts["funds"] != rel.expected["funds"]:
        raise ReleaseError(f"funds: parsed {rel.counts['funds']}, file says {rel.expected['funds']}")
    with open(os.path.join(out_dir, "stg_funds.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(FUND_COLUMNS)
        for r in fund_records:
            w.writerow([r["fund_code"], r["fund_item_id"], _json(r)])

    agreements = read_agreement_hospitals(os.path.join(directory, agreement_name))
    rel.counts["agreement_hospitals"] = len(agreements)
    with open(os.path.join(out_dir, "stg_agreement_hospitals.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(AGREEMENT_COLUMNS)
        for row_no, a in enumerate(agreements, start=1):
            w.writerow([row_no, a["provider_code"], _json(a)])

    rel.change_counts = {
        "funds": count_change_rows(os.path.join(directory, fund_change_name)),
        "products": count_change_rows(os.path.join(directory, product_change_name)),
    }

    seen_ids: set[str] = set()
    registry = Registry()
    total = 0
    with open(os.path.join(out_dir, "stg_products.csv"), "w", encoding="utf-8", newline="") as out:
        w = csv.writer(out)
        w.writerow(PRODUCT_COLUMNS)
        for logical in PRODUCT_FILES:
            path = os.path.join(directory, rel.file_names[logical])
            want_status = "Open" if logical.endswith("Open") else "Closed"
            expected = root_count(path, "Products")
            rel.expected[logical] = expected
            n = 0
            for el in iter_elements(path, "Product"):
                parts = parse_product(el, source_file=logical, fund=_defaults_for(el, fund_defaults),
                                      registry=registry)
                if parts["status"] != want_status:
                    raise ReleaseError(f"{logical}: {parts['gov_product_item_id']} has status {parts['status']}")
                pid = parts["gov_product_item_id"]
                if pid in seen_ids:
                    raise ReleaseError(f"ProductItemID {pid} appears twice in the release")
                seen_ids.add(pid)
                w.writerow([
                    pid, parts["gov_product_id"], parts["gov_product_code"], parts["fund_code"],
                    parts["product_type"],
                    parts["status"], parts["name"], parts["state"], parts["premium_month"],
                    parts["source_file"], _json(parts["raw"]),
                ])
                n += 1
            if n != expected:
                raise ReleaseError(f"{logical}: parsed {n}, file says {expected}")
            rel.counts[logical] = n
            total += n
    rel.counts["products"] = total

    with open(os.path.join(out_dir, "stg_definitions.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(DEFINITION_COLUMNS)
        for def_id, (kind, blob) in registry.definitions.items():
            w.writerow([def_id, kind, blob])
    with open(os.path.join(out_dir, "stg_limit_groups.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(LIMIT_GROUP_COLUMNS)
        for lg_id, blob in registry.limit_groups.items():
            w.writerow([lg_id, blob])
    rel.counts["definitions"] = len(registry.definitions)
    rel.counts["limit_groups"] = len(registry.limit_groups)
    return rel


def _defaults_for(el, fund_defaults):
    code = text(el, "FundCode")
    if code not in fund_defaults:
        raise ReleaseError(f"product {el.get('ProductItemID')} names fund {code!r}, which is not in the Funds file")
    return fund_defaults[code]


def write_manifest(rel: Release, out_dir: str) -> str:
    path = os.path.join(out_dir, "manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "release_date": rel.release_date,
            "sha256": rel.sha256,
            "file_names": rel.file_names,
            "counts": rel.counts,
            "expected": rel.expected,
            "change_counts": rel.change_counts,
        }, f, indent=2, ensure_ascii=False)
    return path
