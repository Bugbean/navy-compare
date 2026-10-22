"""Required evidence, checked once before the model writes.

verify() runs after the draft is written; this closes the gap it cannot close on its own: a figure the
answer must carry that no Fact holds. The data date is the first case - every card and script says
which government snapshot it is from, so a pack without a fact carrying the snapshot is incomplete
before any word is written.

An extensible list of Requirements, not one function: a later need (the member's state, the premium
line on every card) adds its own Requirement to REQUIREMENTS, never a restructure of this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from .facts import EvidencePack, Fact


class Requirement(Protocol):
    name: str

    def missing(self, pack: EvidencePack) -> bool: ...
    def fill(self, pack: EvidencePack) -> Optional[list[Fact]]: ...


@dataclass(frozen=True)
class SnapshotRequirement:
    """Some fact in the pack carries the snapshot date (scope.snapshot). `source` builds that fact when
    it is missing (from the live snapshot row); None when there is nothing to build it from."""
    source: Optional[Callable[[], Optional[Fact]]] = None
    name: str = "snapshot"

    def missing(self, pack: EvidencePack) -> bool:
        return bool(pack.facts) and not any(f.scope.get("snapshot") or f.type == "snapshot"
                                            for f in pack.facts.values())

    def fill(self, pack: EvidencePack) -> Optional[list[Fact]]:
        fact = self.source() if self.source else None
        return [fact] if fact is not None else None


def ensure(pack: EvidencePack, requirements: list[Requirement]) -> list[str]:
    """Fill every currently-missing requirement, in order. Returns the names of those that were missing
    and could not be filled - the caller decides whether that blocks the write."""
    from .facts import QueryCall
    unmet: list[str] = []
    for req in requirements:
        if not req.missing(pack):
            continue
        facts = req.fill(pack)
        if not facts:
            unmet.append(req.name)
            continue
        call = QueryCall(index=len(pack.calls), name=f"guard:{req.name}", args={})
        pack.add_call(call)
        pack.add_facts(facts, call)
    return unmet
