"""Ask for a comparison in plain words: the page finds the policies, the rep confirms, the table opens.

Step 1 (POST /ask): the request is read into a plan (ai/prompts/planner_v1.md). The database finds the
policies it could mean, in groups for Navy Health and the competitors, each with a tick box. Nothing is
compared yet.

Step 2 (POST /ask/compare): the rep ticks the policies they want, from any mix of Navy Health and
competitors. The comparison table opens with Navy Health's policies first, and the features asked
about are shown first.

Every model call is counted through the existing AI accounting (ai/calls.py).
"""
from __future__ import annotations

import logging

from flask import Blueprint, render_template, request

from ai import calls
from ai.client import ROUTER_MODEL, get_client
from app.auth.routes import _check_csrf, csrf_token
from config.reference import CLINICAL_CATEGORIES, EXTRAS_SERVICES
from engine import plan as planning
from engine.compare import compare_many
from engine.db import EngineError, Snapshot

bp = Blueprint("ask", __name__)
log = logging.getLogger("app.ask")
MAX_REQUEST_CHARS = 500
MAX_COLUMNS = 10


def _ints(values: list[str]) -> list[int]:
    out = []
    for v in values:
        try:
            out.append(int(v))
        except ValueError:
            continue
    return out


@bp.route("/ask", methods=["GET", "POST"])
def ask():
    ctx = {"request": "", "error": None, "plan": None, "groups": [], "csrf": csrf_token()}
    if request.method == "POST":
        _check_csrf()
        text = (request.form.get("request") or "").strip()
        ctx["request"] = text
        if not text:
            ctx["error"] = "Type what you want to compare first."
        elif len(text) > MAX_REQUEST_CHARS:
            ctx["error"] = f"Keep it under {MAX_REQUEST_CHARS} characters."
        else:
            accounting, refused = calls.begin("ask", {"chars": len(text)})
            if refused:
                return refused
            try:
                p = planning.plan(text, client=get_client(), model=ROUTER_MODEL)
                groups = planning.candidates(Snapshot(), p)
            except EngineError as exc:
                calls.end(accounting, ROUTER_MODEL, error=exc)
                ctx["error"] = str(exc)
            except Exception as exc:  # the rep sees a plain message; the activity log holds the detail
                log.exception("ask failed")
                calls.end(accounting, ROUTER_MODEL, error=exc)
                ctx["error"] = "That didn't work. Try again, or rephrase the request."
            else:
                calls.end(accounting, ROUTER_MODEL, usage=p.usage,
                          extra={"features": p.features, "state": p.state, "family": p.family_type})
                ctx["plan"] = p
                ctx["groups"] = groups
                if p.confidence < 0.5 and not any(g.policies for g in groups):
                    ctx["error"] = ("I couldn't tell which policies you mean. Try a policy name, a fund, "
                                    "or a feature such as joint replacements or dental.")
    return render_template("ask.html", **ctx)


@bp.route("/ask/compare", methods=["POST"])
def ask_compare():
    _check_csrf()
    ids = _ints(request.form.getlist("policy"))[:MAX_COLUMNS]
    state = request.form.get("state") or "NSW"
    family = request.form.get("family") or "couple"
    features = [f for f in request.form.getlist("feature") if f in CLINICAL_CATEGORIES or f in EXTRAS_SERVICES]
    ctx = {"error": None, "table": None, "features": features, "state": state, "family": family,
           "csrf": csrf_token(), "max_columns": MAX_COLUMNS}
    if not ids:
        ctx["error"] = "Tick at least one policy to compare."
        return render_template("ask_compare.html", **ctx)
    try:
        db = Snapshot()
        policies = db.rows(
            "SELECT p.id, p.current_name, p.product_type, p.is_navy, f.name AS fund_name "
            "FROM policies p JOIN funds f ON f.fund_code = p.fund_code WHERE p.id = ANY(ARRAY["
            + ",".join(str(i) for i in ids) + "]::bigint[])")
        ctx["table"] = compare_many(db, policies=policies, state=state, family=family, scope="")
    except EngineError as exc:
        ctx["error"] = str(exc)
    return render_template("ask_compare.html", **ctx)
