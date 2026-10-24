"""Reads config/materiality.yaml and scores a difference by it.

The file is checked when it is loaded: every tag must name a real feature and every type must have
a rule, so an edit Navy Health makes that the engine cannot follow fails at start-up, not as a
quietly wrong ranking on a call.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

from config.features import FEATURES, item_service

PATH = Path(__file__).resolve().parent.parent / "config" / "materiality.yaml"
TYPES = ("premium", "cover_category", "tier", "wait_months", "copayment", "excess", "extras_service",
         "extras_item", "extras_limit", "ambulance", "accommodation", "known_gap")


class MaterialityError(ValueError):
    pass


@lru_cache(maxsize=1)
def load(path: Optional[str] = None) -> dict[str, Any]:
    with open(path or PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    missing = [t for t in TYPES if t not in cfg]
    if missing:
        raise MaterialityError(f"materiality.yaml has no rule for {missing}")
    if sorted(cfg.get("type_priority", [])) != sorted(TYPES):
        raise MaterialityError("type_priority must list every difference type exactly once")
    chip_keys = {c["key"] for c in cfg.get("chips", [])}
    for tag, keys in (cfg.get("tags") or {}).items():
        if tag not in chip_keys:
            raise MaterialityError(f"tag {tag!r} has no chip")
        bad = [k for k in keys if k not in FEATURES]
        if bad:
            raise MaterialityError(f"tag {tag!r} names unknown features {bad}")
    return cfg


def _banded(rule: dict, size: float) -> int:
    if size <= float(rule.get("noise", 0)):
        return 0
    for band in rule["bands"]:
        if "below" not in band or size < float(band["below"]):
            return int(band["score"])
    return 0


def score(kind: str, size: Optional[float], cfg: Optional[dict] = None) -> int:
    """The materiality of a difference of this kind and size (0 to 3)."""
    cfg = cfg or load()
    rule = cfg[kind]
    if kind == "cover_category":
        return int(rule["two_steps"] if (size or 0) >= 2 else rule["one_step"] if (size or 0) >= 1 else 0)
    if "bands" in rule:
        return _banded(rule, abs(size or 0))
    return int(rule["score"])


def tags_for(key: str, cfg: Optional[dict] = None) -> set[str]:
    """The chips a feature belongs to. An item takes the tags of its extras service."""
    cfg = cfg or load()
    out = set()
    service = item_service(key)
    for tag, keys in (cfg.get("tags") or {}).items():
        if key in keys or (service and (f"extras:{service}" in keys) and key.startswith("item:")):
            out.add(tag)
    return out


def priority(kind: str, cfg: Optional[dict] = None) -> int:
    cfg = cfg or load()
    return cfg["type_priority"].index(kind)


def chips(cfg: Optional[dict] = None) -> list[dict]:
    return list((cfg or load()).get("chips", []))
