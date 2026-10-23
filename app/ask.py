"""The rep's question page: type a question, get the answer and the facts it rests on.

Every model call is counted the way the other AI calls are (ai/calls.py): the person's limit is
checked before the call, and the usage is written to the activity log after it. Navy Health is
always the base of a comparison (see engine/answer.py).
"""
from __future__ import annotations

import logging

from flask import Blueprint, render_template, request

from ai import calls
from ai.client import ROUTER_MODEL, WRITER_MODEL, get_client
from ai.compose import compose
from app.auth.routes import _check_csrf, csrf_token
from engine.answer import answer
from engine.db import Snapshot

bp = Blueprint("ask", __name__)
log = logging.getLogger("app.ask")
MAX_QUESTION_CHARS = 500


@bp.route("/ask", methods=["GET", "POST"])
def ask():
    question, result, error = "", None, None
    if request.method == "POST":
        _check_csrf()
        question = (request.form.get("question") or "").strip()
        if not question:
            error = "Type a question first."
        elif len(question) > MAX_QUESTION_CHARS:
            error = f"Keep the question under {MAX_QUESTION_CHARS} characters."
        else:
            ctx, refused = calls.begin("ask", {"chars": len(question)})
            if refused:
                return refused
            try:
                result = answer(question, db=Snapshot(), client=get_client(), model=ROUTER_MODEL,
                                compose=compose, context={})
            except Exception as exc:  # the rep sees a plain message; the activity log holds the detail
                log.exception("ask failed")
                calls.end(ctx, WRITER_MODEL, error=exc)
                error = "That didn't work. Try again, or rephrase the question."
            else:
                calls.end(ctx, WRITER_MODEL, usage=result.writer_usage,
                          extra={"outcome": result.outcome, "question_type": result.question_type,
                                 "router_usage": _usage_dict(result.router_usage)})
    return render_template("ask.html", question=question, result=result, error=error, csrf=csrf_token())


def _usage_dict(usage) -> dict | None:
    if usage is None:
        return None
    return {"input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None)}

