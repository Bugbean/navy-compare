"""Run records for every scheduled job, and the one place a job's failure is noticed.

    from jobs.runner import job_run

    with job_run("ingest") as run:
        ...                                   # the job's work
        run.detail["records"] = 130_412       # anything worth seeing on the admin page

A job_runs row is written as 'running' when the block starts and closed as 'success' or 'failed' (with
the exception text) when it ends; the exception still propagates, so Cloud Run sees the job fail. Raise
JobSkipped for "nothing to do" (no new release) to close the row as 'skipped' and exit cleanly.

Every outcome passes through record_finish, so it is the one place that can notice a job changing state,
and the one place an alert belongs: an email when a job starts failing, one when it recovers, and
nothing in between however many runs fail.
"""
from __future__ import annotations

import logging
import os
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Optional

log = logging.getLogger("jobs")

ALERT_TO = [a.strip() for a in os.environ.get("JOB_ALERT_EMAILS", "").split(",") if a.strip()]


class JobSkipped(Exception):
    """Nothing to do this run (no new government release, say). Not a failure."""


@dataclass
class Run:
    job_name: str
    row_id: Optional[int]
    started_at: str
    detail: dict = field(default_factory=dict)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sb():
    from app.db import get_sb
    return get_sb()


def record_start(sb, job_name: str) -> Run:
    started = _now_iso()
    try:
        res = sb.table("job_runs").insert({"job_name": job_name, "started_at": started,
                                           "status": "running"}).execute()
        row_id = (res.data or [{}])[0].get("id")
    except Exception as e:                              # noqa: BLE001
        # A job must still run when its bookkeeping cannot be written; the failure is in the logs.
        log.error("could not write the job_runs row for %s: %s", job_name, e)
        row_id = None
    return Run(job_name, row_id, started)


def record_finish(sb, run: Run, status: str, error_message: Optional[str] = None) -> None:
    row = {"finished_at": _now_iso(), "status": status, "detail": run.detail or None,
           "error_message": (error_message or "")[:4000] or None}
    try:
        if run.row_id is not None:
            sb.table("job_runs").update(row).eq("id", run.row_id).execute()
        else:
            res = sb.table("job_runs").insert({"job_name": run.job_name, "started_at": run.started_at,
                                               **row}).execute()
            run.row_id = (res.data or [{}])[0].get("id")
    except Exception as e:                              # noqa: BLE001
        log.error("could not close the job_runs row for %s: %s", run.job_name, e)
    try:
        if status == "failed":
            _alert_failed(sb, run, error_message)
        elif status == "success":
            _alert_recovered(sb, run)
    except Exception as e:                              # noqa: BLE001
        log.error("alerting failed for %s: %s", run.job_name, e)


@contextmanager
def job_run(job_name: str, sb=None) -> Iterator[Run]:
    sb = sb or _sb()
    run = record_start(sb, job_name)
    log.info("%s started (job_runs %s)", job_name, run.row_id)
    try:
        yield run
    except JobSkipped as e:
        run.detail.setdefault("reason", str(e) or "nothing to do")
        record_finish(sb, run, "skipped")
        log.info("%s skipped: %s", job_name, e)
        return
    except BaseException as e:
        record_finish(sb, run, "failed", f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
        raise
    record_finish(sb, run, "success")
    log.info("%s finished", job_name)


def _was_already_failing(sb, job_name: str, before_id: Optional[int]) -> bool:
    """Was this job's previous finished run also a failure? Keyed on the row id rather than a
    timestamp: two runs in the same second are possible, and this must not read its own row."""
    if before_id is None:
        return True          # cannot tell - stay quiet rather than risk a flood
    try:
        rows = (sb.table("job_runs").select("status")
                .eq("job_name", job_name).lt("id", before_id).neq("status", "running")
                .order("id", desc=True).limit(1).execute().data or [])
    except Exception:                                   # noqa: BLE001
        return True
    return bool(rows) and rows[0].get("status") == "failed"


def _notify(subject: str, body_html: str) -> None:
    try:
        from app.auth import mailer
        if not (mailer.configured() and ALERT_TO):
            log.warning("no RESEND_API_KEY or JOB_ALERT_EMAILS on this job — alert not sent: %s", subject)
            return
        ok = mailer.send(ALERT_TO, subject, body_html)
        log.info("alert %s: %s", "sent" if ok else "REJECTED", subject)
    except Exception as e:                              # noqa: BLE001
        # Never let a notification failure change the job's outcome.
        log.error("could not send the alert (%s): %s", subject, e)


def _alert_failed(sb, run: Run, error: Optional[str]) -> None:
    if _was_already_failing(sb, run.job_name, run.row_id):
        log.info("%s was already failing — not emailing again", run.job_name)
        return
    first_line = (error or "").splitlines()[0][:500] if error else ""
    _notify(f"Policy Compare: {run.job_name} failed",
            f"<p><b>{run.job_name}</b> failed at {_now_iso()}.</p>"
            + (f"<p><code>{first_line}</code></p>" if first_line else "")
            + "<p>The full error is on the job's row in job_runs and in Cloud Run's logs.</p>"
            + "<p style='color:#666;font-size:12px'>One email per failure, and one when it recovers. "
              "You will not get this again until it does.</p>")


def _alert_recovered(sb, run: Run) -> None:
    if run.row_id is None or not _was_already_failing(sb, run.job_name, run.row_id):
        return
    _notify(f"Policy Compare: {run.job_name} is working again",
            f"<p><b>{run.job_name}</b> succeeded at {_now_iso()} after failing.</p>")
