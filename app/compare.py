"""The feature comparison page: Navy Health's policy beside a competitor's, feature by feature.

Deterministic: the table is read from the snapshot (engine/compare.py), with no model call.
Names are resolved the same way as the question page, and an ambiguous name is never guessed.
"""
from __future__ import annotations

from flask import Blueprint, render_template, request

from engine import answer as ans
from engine.compare import compare
from engine.db import EngineError, Snapshot

bp = Blueprint("compare", __name__)
STATES = ("NSW", "VIC", "QLD", "SA", "WA", "TAS", "NT", "ACT")
FAMILIES = ("single", "couple", "family", "single_parent", "dependants_only")
TITLES = ("Child", "NonClassified", "Student", "NonStudent", "ConditionalNonStudent", "Disability")


@bp.route("/compare", methods=["GET"])
def compare_page():
    navy_name = (request.args.get("navy") or "").strip()
    comp_name = (request.args.get("comp") or "").strip()
    state = request.args.get("state") or "NSW"
    family = request.args.get("family") or "couple"
    titles = sorted(t for t in request.args.getlist("titles") if t in TITLES)
    ctx = {"navy": navy_name, "comp": comp_name, "state": state, "family": family, "titles": titles,
           "states": STATES, "families": FAMILIES, "dependant_titles": TITLES, "table": None,
           "clarify": [], "error": None}
    if not (navy_name or comp_name):
        return render_template("compare.html", **ctx)
    try:
        db = Snapshot()
        navy, navy_cands = ans.find_policy(db, navy_name, navy=True) if navy_name else (None, [])
        comp, comp_cands = ans.find_policy(db, comp_name, navy=False) if comp_name else (None, [])
        if navy_name and navy is None:
            ctx["clarify"].append(("Navy Health policy", [c["current_name"] for c in navy_cands[:5]]))
        if comp_name and comp is None:
            ctx["clarify"].append(("competitor policy", [c["current_name"] for c in comp_cands[:5]]))
        if navy is None or comp is None:
            return render_template("compare.html", **ctx)
        ctx["table"] = compare(db, navy=navy, comp=comp, state=state, family=family, scope="+".join(titles))
    except EngineError as exc:
        ctx["error"] = str(exc)
    return render_template("compare.html", **ctx)
