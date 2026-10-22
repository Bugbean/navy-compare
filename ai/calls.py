"""Every paid AI call goes through here: the person's limit, the activity log row, the cost, the alert.

    ctx, refused = begin("script", {"call_id": ...})     # limit checked, ai_question row written
    if refused: return refused                             # a 429 the page can show
    ... the model call ...
    end(ctx, model, usage)                                 # tokens, cost and latency on the same row

The limit is counted from activity_log (app.auth.check_ai_limit), so it holds across every Cloud Run
instance. When AI starts failing the admins get one email, then quiet for half an hour, however many
calls fail - the quiet period is shared through activity_log too.

ai_calls (the per-call audit record the plan specifies: facts given, raw and verified output, what the
verifier dropped) is a separate table written by the call screen; this module is the accounting.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

from flask import g, jsonify
from markupsafe import escape

from app import auth

log = logging.getLogger("app.ai")

ALERT_QUIET_MIN = 30
ALERT_TO = [a.strip() for a in os.environ.get("AI_ALERT_EMAILS", "").split(",") if a.strip()]


def begin(kind: str, detail: dict):
    """Check the person's limit, then log the request. Call right before the model call, after input
    validation, so a malformed request doesn't use up quota.

    Returns (ctx, None) to go ahead, or (None, response) to refuse with."""
    from app.auth.routes import log_activity
    user = getattr(g, "user", None)
    detail = {"kind": kind, **detail}
    lim = auth.check_ai_limit(user)
    if not lim.allowed:
        log_activity("ai", "ai_rate_limited", status=429,
                     detail={**detail, "window": lim.window, "limit": lim.limit, "used": lim.used})
        resp = jsonify({"error": lim.message})
        resp.status_code = 429
        resp.headers["Retry-After"] = str(lim.retry_after)
        return None, resp
    return {"id": log_activity("ai", "ai_question", detail=detail), "detail": detail, "t0": time.time()}, None


def end(ctx: dict, model: str, usage: Any = None, error: Optional[Exception] = None,
        extra: Optional[dict] = None) -> None:
    """Complete the row begin() wrote with tokens, cost and latency. `usage` is an SDK Usage, a dict
    with the same field names, or ai.compose.Usage."""
    latency = int((time.time() - ctx["t0"]) * 1000)
    detail = {**ctx["detail"], **(extra or {})}
    if error is not None:
        auth.update_event(ctx["id"], event="ai_error", status=502, latency_ms=latency,
                          ai_model=model, detail={**detail, "error": str(error)[:500]})
        auth.run_in_background(alert, ctx["detail"].get("kind", "ai"), error)
        return
    i, o, w, r = _usage_parts(usage)
    cols, split = auth.ai_usage(model, i, o, w, r)
    auth.update_event(ctx["id"], status=200, latency_ms=latency, detail={**detail, "usage": split}, **cols)


def _usage_parts(usage: Any) -> tuple[int, int, int, int]:
    if usage is None:
        return 0, 0, 0, 0
    if hasattr(usage, "cache_read") and hasattr(usage, "cache_write"):      # ai.compose.Usage
        return usage.input_tokens, usage.output_tokens, usage.cache_write, usage.cache_read

    def get(k: str) -> int:
        v = usage.get(k) if isinstance(usage, dict) else getattr(usage, k, None)
        return v or 0
    return (get("input_tokens"), get("output_tokens"),
            get("cache_creation_input_tokens"), get("cache_read_input_tokens"))


def alert(where: str, exc: Exception) -> None:
    """Email the admins the first time AI starts failing, then stay quiet for half an hour so a broken
    model or key doesn't send hundreds. Never raises."""
    try:
        if not ALERT_TO:
            return
        if not auth.should_alert(where, ALERT_QUIET_MIN, service="web",
                                 detail={"error": f"{type(exc).__name__}: {str(exc)[:300]}"}):
            return
        from app.auth.routes import PUBLIC_BASE_URL, _email_html, _send_email
        _send_email(
            ALERT_TO, "Policy Compare: an AI call failed",
            _email_html(
                "An AI call failed",
                f"<strong>{escape(where)}</strong> could not reach Claude:"
                f"<br><br><code>{escape(type(exc).__name__)}: {escape(str(exc)[:400])}</code>"
                f"<br><br>Reps see the card and the facts without a script until it's fixed. "
                f"Every failure is listed under Admin, Activity (event <em>ai_error</em>).",
                button="Open activity", url=f"{PUBLIC_BASE_URL}/admin/activity?event=ai_error",
                note_html=f"One email per {ALERT_QUIET_MIN} minutes, however many calls fail."))
    except Exception as alert_exc:                      # noqa: BLE001
        log.error("could not send the AI failure alert: %s", alert_exc)


def failed(where: str, exc: Exception, message: str, status: int = 502):
    """An AI call fell over. Log it where Cloud Run can see it, email the admins, and tell an admin on
    screen what actually happened."""
    log.error("%s failed: %s: %s", where, type(exc).__name__, exc)
    auth.run_in_background(alert, where, exc)
    body = {"error": message}
    if getattr(getattr(g, "user", None), "is_admin", False):
        body["detail"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    return jsonify(body), status
