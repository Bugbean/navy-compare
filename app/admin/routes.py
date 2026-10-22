"""
Admin monitoring: the overview, the full activity log, and AI usage per
person. Account management (the Users list and each person's page) lives in
auth_routes.py; this module reads what activity_log has recorded.

Every route re-checks admin rights itself, on top of _authorise() in
app/__init__.py refusing /admin/* to non-admins, so a routing change can't
silently expose the log.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, redirect, render_template, request, url_for
from postgrest.exceptions import APIError

from app import auth
from app.auth.routes import _check_csrf, _require_admin, _sb

bp = Blueprint("admin", __name__)
log = logging.getLogger(__name__)

SYDNEY = ZoneInfo("Australia/Sydney")
PAGE_SIZE = 50

# Plain-English names for the admin, who shouldn't need to read event codes.
EVENT_LABELS = {
    "sign_in": "Signed in",
    "sign_in_failed": "Failed sign-in",
    "sign_out": "Signed out",
    "email_link_failed": "Email link rejected",
    "magic_link_requested": "Requested sign-in link",
    "password_reset_requested": "Requested password reset",
    "password_reset_link_used": "Opened password reset",
    "password_changed": "Changed password",
    "user_invited": "Invited a user",
    "user_approved": "Approved a user",
    "user_rejected": "Rejected a user",
    "user_suspended": "Suspended a user",
    "user_reinstated": "Reinstated a user",
    "modules_changed": "Changed modules",
    "admin_granted": "Made administrator",
    "admin_revoked": "Removed administrator",
    "limits_changed": "Changed AI limits",
    "access_denied": "Denied access",
    "not_approved": "Blocked: not approved",
    "ai_question": "AI question",
    "ai_rejected_offtopic": "AI: off-topic, refused",
    "ai_rate_limited": "AI: over limit",
    "ai_error": "AI: failed",
    "page_view": "Page view",
}

# Shown with a warning marker: the events worth a second look.
SECURITY_EVENTS = {
    "sign_in_failed", "access_denied", "not_approved",
    "ai_rate_limited", "email_link_failed", "admin_granted", "admin_revoked",
}

CATEGORIES = ("auth", "admin", "access", "ai", "page", "feedback")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_EVENT = re.compile(r"^[a-z_]{1,40}$")


# ── Template helpers ──────────────────────────────────────────────────────────

def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


@bp.app_template_filter("syd")
def syd(value, fmt: str = "%d %b %Y, %-I:%M %p") -> str:
    """ISO timestamp from the database → Sydney local time."""
    dt = _parse_ts(value)
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(SYDNEY)
    # %-I (no leading zero) isn't portable to Windows' strftime.
    return local.strftime(fmt.replace("%-I", str(int(local.strftime("%I")))))


@bp.app_template_filter("usd")
def usd(value) -> str:
    v = float(value or 0)
    if v == 0:
        return "$0.00"
    return f"${v:,.4f}" if v < 0.01 else f"${v:,.2f}"


@bp.app_template_global("event_label")
def event_label(event: str) -> str:
    return EVENT_LABELS.get(event, event.replace("_", " ").capitalize())


@bp.app_template_global("is_security")
def is_security(event: str) -> bool:
    return event in SECURITY_EVENTS


@bp.app_template_global("row_summary")
def row_summary(row: dict) -> str:
    """One line saying what a log row was about. Rendered through Jinja's
    autoescape: every value here can contain text a user typed."""
    d = row.get("detail") or {}
    ev = row.get("event", "")
    if row.get("category") == "ai":
        text = d.get("question") or ""
        if ev == "ai_rate_limited":
            return f"{d.get('used')}/{d.get('limit')} per {d.get('window')} · {text[:120]}"
        if ev == "ai_rejected_offtopic":
            return f"{text[:120]} ({d.get('reason', '')})"
        return text[:160]
    if row.get("category") == "admin":
        who = d.get("target_email", "")
        if ev == "modules_changed":
            parts = []
            if d.get("added"):
                parts.append("+" + ", ".join(d["added"]))
            if d.get("removed"):
                parts.append("−" + ", ".join(d["removed"]))
            return f"{who}: {' '.join(parts)}"
        if ev == "limits_changed":
            to = d.get("to") or {}
            fmt = "/".join(str(to.get(f"ai_limit_{w}") if to.get(f"ai_limit_{w}") is not None else "default")
                           for w in ("minute", "hour", "day"))
            return f"{who}: {fmt} (min/hour/day)"
        return who
    if ev == "access_denied":
        return f"{row.get('path') or ''} (module: {d.get('module') or d.get('area') or 'unmapped'})"
    if ev in ("sign_in", "sign_in_failed"):
        bits = [d.get("method") or ""]
        if d.get("reason"):
            bits.append(d["reason"])
        return " · ".join(b for b in bits if b)
    return row.get("path") or ""


# ── Shared ────────────────────────────────────────────────────────────────────

def _sydney_midnight() -> datetime:
    now = datetime.now(SYDNEY)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


PERIODS = {
    "today": ("Today", lambda: _sydney_midnight()),
    "7d":    ("Last 7 days", lambda: datetime.now(timezone.utc) - timedelta(days=7)),
    "30d":   ("Last 30 days", lambda: datetime.now(timezone.utc) - timedelta(days=30)),
}


def _usage_since(sb, since: datetime) -> list[dict]:
    return sb.rpc("fn_ai_usage_by_user", {"p_since": since.isoformat()}).execute().data or []


def user_activity_summary(sb, user_id: str) -> dict:
    """What the person's own admin page shows under their access settings."""
    recent = sb.table("activity_log").select("*").eq("user_id", user_id) \
               .order("occurred_at", desc=True).limit(25).execute().data
    last = sb.table("activity_log").select("occurred_at,detail").eq("user_id", user_id) \
             .eq("event", "sign_in").order("occurred_at", desc=True).limit(1).execute().data
    usage = {}
    for key, (label, since) in PERIODS.items():
        row = next((r for r in _usage_since(sb, since()) if r.get("user_id") == user_id), None)
        usage[key] = {"label": label, **(row or {"questions": 0, "offtopic": 0, "rate_limited": 0,
                                                 "errors": 0, "tokens_in": 0, "tokens_out": 0,
                                                 "cost_usd": 0})}
    return {"recent": recent, "last_sign_in": last[0] if last else None, "usage": usage}


# ── Overview ──────────────────────────────────────────────────────────────────

@bp.route("/admin")
def overview():
    _require_admin()
    sb = _sb()
    ov = sb.rpc("fn_admin_overview", {}).execute().data or {}
    pending = sb.table("user_profiles").select("id,full_name,email,created_at") \
                .eq("status", "pending").not_.is_("confirmed_at", "null") \
                .order("created_at").limit(10).execute().data
    watch = sorted(SECURITY_EVENTS | {"user_approved", "user_rejected", "user_suspended",
                                      "user_reinstated", "modules_changed", "limits_changed"})
    recent = sb.table("activity_log").select("*").in_("event", watch) \
               .order("occurred_at", desc=True).limit(15).execute().data
    # Amber thresholds: the patterns that suggest someone guessing passwords
    # or probing for pages they weren't given.
    alerts = {
        "failed": (ov.get("top_failed_count") or 0) >= 5,
        "denied": (ov.get("top_denied_count") or 0) >= 5,
        "limited": (ov.get("rate_limited_24h") or 0) >= 3,
        "pending": (ov.get("pending_users") or 0) > 0,
    }

    # Are the scheduled jobs running? A job can fail run after run with nothing
    # saying so anywhere. This tile is cheap — one read of job_runs — and puts it
    # on the page an admin already opens, rather than only on the page they would
    # have to think to open.
    pipeline = {"failing": [], "last": None, "ok": True}
    try:
        rows = (sb.table("job_runs").select("job_name,status,started_at")
                .order("started_at", desc=True).limit(120).execute().data or [])
        if rows:
            pipeline["last"] = rows[0]["started_at"]
        settled: set = set()
        streak: dict = {}
        for r in rows:
            name = r["job_name"]
            if name in settled:
                continue
            if r["status"] == "success":
                settled.add(name)
            else:
                streak[name] = streak.get(name, 0) + 1
        pipeline["failing"] = sorted(streak.items(), key=lambda kv: -kv[1])
        pipeline["ok"] = not streak
    except Exception:                                   # noqa: BLE001
        pipeline["ok"] = None                            # unknown, not "fine"

    from app.freshness import check_all
    return render_template("admin/overview.html", ov=ov, pending=pending, recent=recent,
                           alerts=alerts, defaults=auth.DEFAULT_AI_LIMITS,
                           pipeline=pipeline, freshness=check_all(sb))


# ── Activity log ──────────────────────────────────────────────────────────────

def _day_bound(value: str, end: bool = False) -> datetime | None:
    """A YYYY-MM-DD date from the filter form, as the start (or end) of that
    day in Sydney."""
    try:
        d = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=SYDNEY)
    except (TypeError, ValueError):
        return None
    return d + timedelta(days=1) if end else d


@bp.route("/admin/activity")
def activity():
    _require_admin()
    sb = _sb()
    args = request.args
    f = {
        "q": args.get("q", "").strip()[:100],
        "user": args.get("user", "") if _UUID.match(args.get("user", "")) else "",
        "category": args.get("category", ""),
        "event": args.get("event", "") if _EVENT.match(args.get("event", "")) else "",
        "from": args.get("from", "") if _day_bound(args.get("from", "")) else "",
        "to": args.get("to", "") if _day_bound(args.get("to", "")) else "",
    }
    try:
        page = max(1, min(int(args.get("page", 1)), 10000))
    except ValueError:
        page = 1

    query = sb.table("activity_log").select("*", count="exact")
    if f["category"] in CATEGORIES:
        query = query.eq("category", f["category"])
    elif f["category"] != "all":
        f["category"] = ""
        query = query.neq("category", "page")  # default view: page views would bury the rest
    if f["event"]:
        query = query.eq("event", f["event"])
    if f["user"]:
        query = query.eq("user_id", f["user"])
    if f["q"]:
        # Literal match on the address: escape the pattern characters so a
        # stray % or _ in the search box can't widen it.
        pat = f["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.ilike("user_email", f"%{pat}%")
    if f["from"]:
        query = query.gte("occurred_at", _day_bound(f["from"]).isoformat())
    if f["to"]:
        query = query.lt("occurred_at", _day_bound(f["to"], end=True).isoformat())

    off = (page - 1) * PAGE_SIZE
    try:
        res = query.order("occurred_at", desc=True).range(off, off + PAGE_SIZE - 1).execute()
    except APIError as e:
        # PostgREST refuses a range past the end (a stale "Older" link, or a
        # filter that now matches less). Start again from the first page.
        if e.code == "PGRST103" and page > 1:
            return redirect(url_for("admin.activity", **{k: v for k, v in f.items() if v}))
        raise
    total = res.count or 0

    filter_user = None
    if f["user"]:
        rows = sb.table("user_profiles").select("full_name,email").eq("id", f["user"]).execute().data
        filter_user = rows[0] if rows else {"email": f["user"]}

    def page_url(n):
        return url_for("admin.activity", **{k: v for k, v in f.items() if v}, page=n)

    return render_template(
        "admin/activity.html", rows=res.data, f=f, total=total, page=page,
        pages=max(1, -(-total // PAGE_SIZE)), first=off + 1 if total else 0,
        last=min(off + PAGE_SIZE, total), page_url=page_url, filter_user=filter_user,
        categories=CATEGORIES, events=sorted(EVENT_LABELS, key=event_label))


# ── AI usage ──────────────────────────────────────────────────────────────────

_SORTS = {"cost": "cost_usd", "questions": "questions", "tokens": "tokens_in",
          "limited": "rate_limited", "offtopic": "offtopic", "last": "last_at"}


@bp.route("/admin/usage")
def usage():
    _require_admin()
    sb = _sb()
    period = request.args.get("period", "7d")
    if period not in PERIODS:
        period = "7d"
    sort = request.args.get("sort", "cost")
    if sort not in _SORTS:
        sort = "cost"

    rows = _usage_since(sb, PERIODS[period][1]())
    key = _SORTS[sort]
    rows.sort(key=lambda r: (r.get(key) is not None, float(r.get(key) or 0)
                             if key != "last_at" else str(r.get(key) or "")), reverse=True)

    # Each person's limits, so a heavy user can be compared with their cap.
    ids = [r["user_id"] for r in rows if r.get("user_id")]
    limits = {}
    if ids:
        for p in sb.table("user_profiles").select("id,ai_limit_minute,ai_limit_hour,ai_limit_day") \
                   .in_("id", ids).execute().data:
            limits[p["id"]] = {w: p.get(f"ai_limit_{w}") if p.get(f"ai_limit_{w}") is not None
                               else auth.DEFAULT_AI_LIMITS[w] for w in ("minute", "hour", "day")}

    totals = {k: sum(float(r.get(k) or 0) for r in rows)
              for k in ("questions", "offtopic", "rate_limited", "errors", "tokens_in", "tokens_out", "cost_usd")}
    return render_template("admin/usage.html", rows=rows, totals=totals, period=period,
                           periods=PERIODS, sort=sort, limits=limits,
                           defaults=auth.DEFAULT_AI_LIMITS)


# ── Feedback ─────────────────────────────────────────────────────────────────
# Requests and bug reports from the people using the app. They are emailed to
# the maintainers as they arrive; this is the same reports as a list, which an
# inbox is not — it can be counted, filtered by page, and marked done.

FEEDBACK_PAGE_SIZE = 25

# The default view is what is still outstanding. A list whose first screen is
# mostly closed items stops being looked at.
FEEDBACK_VIEWS = {
    "open":     ("Open", ["new", "triaged"]),
    "new":      ("New", ["new"]),
    "triaged":  ("In progress", ["triaged"]),
    "done":     ("Done", ["done"]),
    "declined": ("Declined", ["declined"]),
    "all":      ("Everything", []),
}

STATUS_LABELS = {"new": "New", "triaged": "In progress",
                 "done": "Done", "declined": "Declined"}

KIND_LABELS = {"request": "Request", "bug": "Bug"}


@bp.route("/admin/feedback")
def feedback():
    _require_admin()
    sb = _sb()
    args = request.args

    view = args.get("view", "open")
    if view not in FEEDBACK_VIEWS:
        view = "open"
    kind = args.get("kind", "")
    if kind not in KIND_LABELS:
        kind = ""
    page_key = args.get("page_key", "")[:60]
    try:
        page = max(1, min(int(args.get("page", 1)), 10000))
    except ValueError:
        page = 1

    query = sb.table("feedback").select("*", count="exact")
    statuses = FEEDBACK_VIEWS[view][1]
    if statuses:
        query = query.in_("status", statuses)
    if kind:
        query = query.eq("kind", kind)
    if page_key:
        query = query.eq("page_key", page_key)

    off = (page - 1) * FEEDBACK_PAGE_SIZE
    try:
        res = (query.order("created_at", desc=True)
               .range(off, off + FEEDBACK_PAGE_SIZE - 1).execute())
    except APIError as e:
        # A stale "Older" link, or a filter that now matches less. Same
        # recovery as the activity log: start again from the first page.
        if e.code == "PGRST103" and page > 1:
            return redirect(url_for("admin.feedback", view=view, kind=kind))
        raise
    rows, total = res.data or [], res.count or 0

    # How many are outstanding, regardless of the current filter — the number
    # worth knowing is "what is waiting", not "what is on this screen".
    try:
        open_count = (sb.table("feedback").select("id", count="exact")
                      .in_("status", ["new", "triaged"]).limit(1).execute().count or 0)
    except Exception:                                   # noqa: BLE001
        open_count = None

    # Which pages people actually report against, for the filter. Drawn from
    # the reports themselves so it never offers a page nobody has mentioned.
    try:
        seen = (sb.table("feedback").select("page_key")
                .order("created_at", desc=True).limit(500).execute().data or [])
        page_keys = sorted({r["page_key"] for r in seen if r.get("page_key")})
    except Exception:                                   # noqa: BLE001
        page_keys = []

    def page_url(n):
        return url_for("admin.feedback", view=view, kind=kind or None,
                       page_key=page_key or None, page=n)

    return render_template(
        "admin/feedback.html", rows=rows, total=total, page=page,
        pages=max(1, -(-total // FEEDBACK_PAGE_SIZE)),
        first=off + 1 if total else 0, last=min(off + FEEDBACK_PAGE_SIZE, total),
        page_url=page_url, view=view, kind=kind, page_key=page_key,
        views=FEEDBACK_VIEWS, kinds=KIND_LABELS, statuses=STATUS_LABELS,
        page_keys=page_keys, open_count=open_count)


@bp.route("/admin/feedback/<int:report_id>", methods=["POST"])
def feedback_update(report_id):
    """Move a report along, and keep a note of why.

    The report itself is never edited — what somebody wrote is the record, and
    an admin rewriting it would destroy the only first-hand account of the
    fault. Only the status and the admin's own note can change.
    """
    admin = _require_admin()
    _check_csrf()
    sb = _sb()

    status = request.form.get("status", "")
    if status not in STATUS_LABELS:
        abort(400, "Unknown status.")
    notes = (request.form.get("notes") or "").strip()[:2000]

    rows = (sb.table("feedback").select("status,kind,page_key,user_email")
            .eq("id", report_id).execute().data)
    if not rows:
        abort(404)
    before = rows[0]

    sb.table("feedback").update({"status": status, "notes": notes or None}) \
        .eq("id", report_id).execute()

    auth.log_event(
        service="web", category="feedback", event="feedback_updated", user=admin,
        detail={"id": report_id, "from": before["status"], "to": status,
                "kind": before.get("kind"), "page": before.get("page_key")})

    return redirect(url_for("admin.feedback",
                            **{k: v for k, v in request.args.items() if v}))
