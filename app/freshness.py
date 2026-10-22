"""The freshness register: everything scheduled, and whether it has run recently enough.

The dangerous failures are the silent ones: a job that stops and every figure on the screen quietly
stops moving. So each scheduled thing has an entry here from day one, and the admin home page asks, on
every load, whether it is current. There is no stored status to go stale - a cached freshness page is
a page that can say everything is fine after it has stopped.

An entry is checked against the newest successful job_runs row for its job: older than max_age is
'stale'; never succeeded is 'missing'; the newest run failing (even if an older one succeeded) is
'failing'. Entries that are not a job (the in-process search index, the AI being reachable) supply
their own `check` and report the same states.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional


@dataclass(frozen=True)
class Entry:
    key: str
    label: str
    max_age: timedelta
    job_name: Optional[str] = None
    runs: str = ""
    # For entries that are not a job: returns the time it was last known good, or None.
    check: Optional[Callable[[], Optional[datetime]]] = None


# Plan, Observability: the register. Jobs that do not exist yet are listed so the page shows them
# 'missing' until they do - which is the truth.
REGISTER: list[Entry] = [
    Entry("release_check", "Government release check", timedelta(hours=26), job_name="release_check",
          runs="Nightly, 02:00 AEST"),
    Entry("snapshot_live", "Live snapshot", timedelta(days=35), job_name="ingest",
          runs="When a new monthly release is found"),
    Entry("comparison_rebuild", "Comparison rebuild", timedelta(days=35), job_name="rebuild",
          runs="After each snapshot is promoted"),
]


@dataclass
class Status:
    entry: Entry
    state: str                     # ok | stale | missing | failing | unknown
    last_ok: Optional[str] = None
    last_status: Optional[str] = None
    detail: str = ""


def _parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def check_all(sb, now: Optional[datetime] = None, register: Optional[list[Entry]] = None) -> list[Status]:
    """One status per entry. One read of job_runs covers every job entry."""
    now = now or datetime.now(timezone.utc)
    register = register if register is not None else REGISTER
    runs: dict[str, dict] = {}
    try:
        rows = (sb.table("job_runs").select("job_name,status,started_at,finished_at")
                .neq("status", "running").order("started_at", desc=True).limit(500).execute().data or [])
        for r in rows:
            j = runs.setdefault(r["job_name"], {"last": r, "last_ok": None})
            if r["status"] == "success" and j["last_ok"] is None:
                j["last_ok"] = r
    except Exception as exc:                            # noqa: BLE001
        return [Status(e, "unknown", detail=f"job_runs could not be read: {type(exc).__name__}") for e in register]

    out: list[Status] = []
    for e in register:
        if e.check is not None:
            try:
                good = e.check()
            except Exception as exc:                    # noqa: BLE001
                out.append(Status(e, "unknown", detail=f"check failed: {type(exc).__name__}"))
                continue
            if good is None:
                out.append(Status(e, "missing", detail="never known good"))
            elif now - good > e.max_age:
                out.append(Status(e, "stale", last_ok=good.isoformat(), detail="older than allowed"))
            else:
                out.append(Status(e, "ok", last_ok=good.isoformat()))
            continue
        j = runs.get(e.job_name or "")
        if j is None:
            out.append(Status(e, "missing", detail="has never run"))
            continue
        last, last_ok = j["last"], j["last_ok"]
        ok_at = _parse((last_ok or {}).get("finished_at") or (last_ok or {}).get("started_at"))
        st = Status(e, "ok", last_ok=ok_at.isoformat() if ok_at else None, last_status=last["status"])
        if last["status"] == "failed":
            st.state, st.detail = "failing", "the newest run failed"
        elif ok_at is None:
            st.state, st.detail = "missing", "has never succeeded"
        elif now - ok_at > e.max_age:
            st.state, st.detail = "stale", f"last succeeded more than {e.max_age} ago"
        out.append(st)
    return out
