"""The smaller ported pieces: name matching, the evidence guard, job run records, freshness,
readability, and AI cost for the current models."""
from datetime import datetime, timedelta, timezone

import pytest

from ai.evidence_guard import SnapshotRequirement, ensure
from ai.facts import EvidencePack
from ai.readability import fk_grade, max_sentence_words
from app import freshness
from app.auth import ai_usage
from identity import name_match as nm
from jobs import runner
from tests import ai_fixtures as fx

# ── name matching ───────────────────────────────────────────────────────────

KNOWN = [("Bupa Silver Plus Advanced", "P9"), ("Bupa Silver Plus Basic", "P8"), ("Medibank Gold Hospital", "P7")]


def test_a_clear_typo_on_a_full_name_is_auto_matched():
    m = nm.best_matches("Medibank Gold Hosptal", KNOWN, id_of=lambda x: x)
    assert nm.safe_auto_match("Medibank Gold Hosptal", m).canonical_id == "P7"


def test_two_close_candidates_are_never_picked_between():
    known = [*KNOWN, ("Bupa Silver Plus Basix", "P6")]
    m = nm.best_matches("Bupa Silver Plus Basi", known, id_of=lambda x: x)
    assert len(m) >= 2 and nm.safe_auto_match("Bupa Silver Plus Basi", m) is None


def test_formatting_is_not_a_spelling_difference():
    assert nm.compact("Silver-Plus  Advanced.") == nm.compact("silver plus advanced")


# ── evidence guard ──────────────────────────────────────────────────────────

def test_a_pack_with_the_snapshot_needs_nothing():
    assert ensure(fx.pack(), [SnapshotRequirement()]) == []


def test_a_missing_snapshot_is_filled_or_reported():
    bare = fx.crown().model_copy(update={"scope": {"state": "NSW"}})
    pack = EvidencePack.of([bare])
    assert ensure(pack, [SnapshotRequirement()]) == ["snapshot"]
    snap = bare.model_copy(update={"fact_id": "S1", "type": "snapshot", "value": "2026-10-01"})
    pack = EvidencePack.of([bare])
    assert ensure(pack, [SnapshotRequirement(source=lambda: snap)]) == [] and "S1" in pack.facts


# ── job runs ────────────────────────────────────────────────────────────────

class Q:
    def __init__(self, sb, name):
        self.sb, self.name, self.op, self.row = sb, name, None, None

    def insert(self, row):
        self.op, self.row = "insert", row
        return self

    def update(self, row):
        self.op, self.row = "update", row
        return self

    def select(self, *a):
        self.op = "select"
        return self

    def __getattr__(self, _):
        return lambda *a, **k: self

    def execute(self):
        if self.op == "insert":
            self.sb.rows.append({"id": len(self.sb.rows) + 1, **self.row})
            return type("R", (), {"data": [self.sb.rows[-1]]})()
        if self.op == "update":
            self.sb.rows[-1].update(self.row)
        return type("R", (), {"data": list(reversed(self.sb.prior))})()


class SB:
    def __init__(self, prior=()):
        self.rows, self.prior = [], list(prior)

    def table(self, name):
        return Q(self, name)


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setattr(runner, "_notify", lambda subject, body: sent.append(subject))
    return sent


def test_a_successful_job_writes_running_then_success(mail):
    sb = SB()
    with runner.job_run("ingest", sb=sb) as run:
        run.detail["records"] = 10
    assert sb.rows[0]["status"] == "success" and sb.rows[0]["detail"] == {"records": 10} and not mail


def test_a_failure_is_recorded_reraised_and_emailed_once(mail):
    sb = SB(prior=[{"status": "success"}])
    with pytest.raises(ValueError):
        with runner.job_run("ingest", sb=sb):
            raise ValueError("bad zip")
    assert sb.rows[0]["status"] == "failed" and "bad zip" in sb.rows[0]["error_message"]
    assert mail == ["Policy Compare: ingest failed"]
    with pytest.raises(ValueError):
        with runner.job_run("ingest", sb=SB(prior=[{"status": "failed"}])):
            raise ValueError("still bad")
    assert len(mail) == 1, "already failing: no second email"


def test_nothing_to_do_is_skipped_not_failed(mail):
    sb = SB()
    with runner.job_run("release_check", sb=sb):
        raise runner.JobSkipped("no new release")
    assert sb.rows[0]["status"] == "skipped" and not mail


# ── freshness ───────────────────────────────────────────────────────────────

NOW = datetime(2026, 10, 23, 9, tzinfo=timezone.utc)


class RunsSB:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        rows = self._rows

        class T:
            def __getattr__(self, _):
                return lambda *a, **k: self

            def execute(self):
                return type("R", (), {"data": rows})()
        return T()


def _at(hours):
    return (NOW - timedelta(hours=hours)).isoformat()


def test_freshness_states():
    rows = [
        {"job_name": "release_check", "status": "success", "started_at": _at(3), "finished_at": _at(3)},
        {"job_name": "ingest", "status": "failed", "started_at": _at(5), "finished_at": _at(5)},
        {"job_name": "ingest", "status": "success", "started_at": _at(700), "finished_at": _at(700)},
    ]
    states = {s.entry.key: s.state for s in freshness.check_all(RunsSB(rows), now=NOW)}
    assert states == {"release_check": "ok", "snapshot_live": "failing", "comparison_rebuild": "missing"}


def test_a_stale_job_is_stale():
    rows = [{"job_name": "release_check", "status": "success", "started_at": _at(30), "finished_at": _at(30)}]
    st = freshness.check_all(RunsSB(rows), now=NOW)[0]
    assert st.state == "stale"


# ── readability and cost ─────────────────────────────────────────────────────

def test_plain_script_scores_low():
    text = "Navy Health pays up to $1,000 on a crown. Bupa Silver Plus pays up to $600."
    assert fk_grade(text) <= 8 and max_sentence_words(text) <= 20


def test_current_models_are_priced():
    assert ai_usage("claude-haiku-5-5", input_tokens=1_000_000)[0]["ai_cost_usd"] == 0.10
    assert ai_usage("claude-sonnet-5-5", output_tokens=1_000_000)[0]["ai_cost_usd"] == 10.0
    assert ai_usage("claude-opus-5-5", input_tokens=1_000_000)[0]["ai_cost_usd"] == 4.0
