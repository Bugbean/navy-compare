"""Facts as the card builds them, for the AI layer's tests: one Navy Health policy against one
competitor, NSW family, from the 1 Oct 2026 snapshot. Invented figures, real shapes."""
from ai.facts import CoverageMeta, EvidencePack, Fact

COMPLETE = CoverageMeta(requested="NSW family, snapshot 2026-10-01", complete_for_claim=True)
NAVY = {"id": "P1", "label": "Navy Health Top Extras", "entity_type": "policy"}
SCOPE = {"state": "NSW", "family_type": "family", "snapshot": "2026-10-01"}


def crown() -> Fact:
    return Fact(fact_id="F3", type="extras_item", subject=NAVY, metric="major_dental_615",
                value={"navy": 1000, "comp": 600}, unit="AUD",
                text="Navy Health pays up to $1,000 on a crown; Bupa Silver Plus pays up to $600",
                names=["Navy Health", "Bupa Silver Plus", "Navy Health Top Extras"], scope=SCOPE,
                coverage=COMPLETE)


def premium() -> Fact:
    return Fact(fact_id="F1", type="premium", subject=NAVY, metric="premium_month",
                value={"navy": 412.35, "comp": 398.10}, unit="AUD/month",
                names=["Navy Health", "Bupa Silver Plus"], scope=SCOPE, coverage=COMPLETE)


def wait() -> Fact:
    return Fact(fact_id="F5", type="wait_months", subject=NAVY, metric="joint_replacement_wait",
                value={"navy": 12, "comp": 12}, unit="months", names=["Navy Health", "Bupa Silver Plus"],
                scope=SCOPE, claimable={"rank": 2, "of": 40},
                coverage=CoverageMeta(requested="open policies", complete_for_claim=False,
                                      reason="closed products are not compared"))


def pack() -> EvidencePack:
    return EvidencePack.of([premium(), crown(), wait()])


NAMES = {"navy health": "NHB", "bupa silver plus": "P9", "bupa": "BUP", "medibank gold": "P7",
         "navy health top extras": "P1"}


def known_names() -> dict[str, str]:
    return dict(NAMES)
