"""The call screen: find policies as you type, say what to compare in plain words, and see which
policy is better on each benefit.

    GET  /call             the screen
    GET  /search           type-ahead: companies and policies matching what has been typed (JSON)
    GET  /search/benefits  type-ahead for benefits ("crown", "physio", "hips") (JSON)
    POST /call/plan        read a plain-words request into policy suggestions and benefits (JSON)
    GET  /card             the comparison for the chosen policies (an HTML fragment)

Navy Health's policies are always the base: they come first, and each competitor gets a card against
each chosen Navy Health policy. Every figure is read from the snapshot (engine/benefits.py) and every
judgement of better or worse comes from engine/differences.py; the model only reads the request.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from flask import Blueprint, jsonify, render_template, request

from ai import calls
from ai.client import ROUTER_MODEL, WRITER_MODEL, get_client
from ai.compose import compose
from ai.facts import EvidencePack, Fact
from app.auth.routes import _check_csrf, csrf_token
from config import features as feats
from config.reference import DEPENDANT_TITLES, FAMILY_TYPES, STATES
from engine import benefits as bn
from engine import catalog as cat
from engine import differences as df
from engine import materiality as mat
from engine import plan as planning
from engine import script as talk
from engine.db import EngineError, Snapshot

bp = Blueprint("call", __name__)
WRITER_PROMPT = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "writer_v1.md"
log = logging.getLogger("app.call")

MAX_POLICIES = 10
MAX_CARDS = 6
MAX_REQUEST_CHARS = 500
REAL_STATES = [s for s in STATES if s != "ALL"]
QUICK_BENEFITS = [
    ("Price and excess", ["price", "excess", "copayment"]),
    ("Waiting periods", ["wait:PreExisting", "wait:Other", "wait:SubAcute", "wait:PregnancyBirth"]),
    ("Hospital basics", ["tier", "accommodation", "ambulance", "known_gap"]),
    ("Joints and back", ["cover:JointReplacements", "cover:JointReconstructions", "cover:BackNeckSpine",
                         "cover:BoneJointMuscle"]),
    ("Pregnancy", ["cover:PregnancyBirth", "wait:PregnancyBirth", "cover:AssistedReproductive"]),
    ("Dental", ["extras:DentalGeneral", "extras:DentalMajor", "limit:DentalGeneral", "limit:DentalMajor",
                "item:DentalGeneral114ScaleClean", "item:DentalMajor615FullCrownVeneered"]),
    ("Physio and chiro", ["extras:Physiotherapy", "limit:Physiotherapy", "item:PhysiotherapyInitial",
                          "extras:Chiropractic", "item:ChiropracticInitial"]),
    ("Optical", ["extras:Optical", "limit:Optical", "item:OpticalSingleVisionLenses"]),
]


def _warm_index() -> None:
    """Build the type-ahead index in the background while the screen loads, so the first keystroke
    does not wait for it."""
    try:
        cat.catalog(Snapshot())
    except Exception:  # the search route reports any real problem when it is used
        log.warning("type-ahead index could not be built in advance", exc_info=True)


@bp.route("/call")
def call_screen():
    threading.Thread(target=_warm_index, daemon=True).start()
    groups = {}
    for f in feats.FEATURES.values():
        groups.setdefault(f.group, []).append({"key": f.key, "label": f.label})
    return render_template("call.html", csrf=csrf_token(), states=REAL_STATES, families=FAMILY_TYPES,
                           titles=DEPENDANT_TITLES, chips=mat.chips(), quick=QUICK_BENEFITS,
                           feature_labels={k: f.label for k, f in feats.FEATURES.items()})


@bp.route("/search")
def search():
    side = request.args.get("side", "any")
    if side not in ("navy", "comp", "any"):
        side = "any"
    product_type = request.args.get("type", "")
    if product_type not in ("", "hospital", "extras", "combined"):
        product_type = ""
    q = (request.args.get("q") or "")[:80]
    fund = (request.args.get("fund") or "")[:3].upper()
    try:
        found = cat.catalog(Snapshot()).search(q, side=side, fund=fund, product_type=product_type,
                                               limit=12 if fund else 8)
    except EngineError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify(found)


@bp.route("/search/benefits")
def search_benefits():
    q = (request.args.get("q") or "")[:60]
    return jsonify({"benefits": [{"key": f.key, "label": f.label, "group": f.group}
                                 for f in feats.search(q, limit=10)]})


@bp.route("/call/plan", methods=["POST"])
def call_plan():
    _check_csrf()
    text = (request.form.get("request") or "").strip()
    if not text:
        return jsonify({"error": "Type what you want to compare."}), 400
    if len(text) > MAX_REQUEST_CHARS:
        return jsonify({"error": f"Keep it under {MAX_REQUEST_CHARS} characters."}), 400
    accounting, refused = calls.begin("plan", {"chars": len(text)})
    if refused:
        return refused
    try:
        p = planning.plan(text, client=get_client(), model=ROUTER_MODEL)
        offer = planning.suggestions(cat.catalog(Snapshot()), p)
    except Exception as exc:  # the rep gets a plain message; the activity log keeps the detail
        log.exception("plan failed")
        calls.end(accounting, ROUTER_MODEL, error=exc)
        return jsonify({"error": "I couldn't read that. Try naming a policy or a company."}), 502
    calls.end(accounting, ROUTER_MODEL, usage=p.usage,
              extra={"features": p.features, "navy_terms": len(p.navy_terms), "comp_terms": len(p.comp_terms)})
    understood = bool(p.navy_terms or p.comp_terms or p.navy_all or p.features)
    return jsonify({
        "understood": understood and p.confidence >= 0.4,
        "navy": offer["navy"], "comp": offer["comp"],
        "features": [{"key": k, "label": feats.FEATURES[k].label} for k in p.features],
        "state": p.state, "family": p.family_type, "product_type": p.product_type,
    })


@dataclass
class Selection:
    ids: list[int]
    state: str
    family: str
    scope: str
    wanted: list[str]
    chips: set[str]
    show_all: bool
    excess: Optional[float]


def _selection(source) -> "Selection | str":
    """What to compare, from a query string or a form. Every value is checked against the enumerations
    and the benefits catalogue; anything else is dropped. Returns an error message instead when the
    state or household is not one the data knows."""
    def items(name: str) -> list[str]:
        return [v for v in (x.strip() for x in (source.get(name) or "").split(",")) if v]

    ids: list[int] = []
    for v in items("ids"):
        if v.isdigit() and int(v) not in ids:
            ids.append(int(v))
    state = source.get("state") or "NSW"
    family = source.get("family") or "couple"
    if state not in REAL_STATES or family not in FAMILY_TYPES:
        return "Choose a state and household."
    titles = sorted(t for t in items("titles") if t in DEPENDANT_TITLES)
    chip_keys = {c["key"] for c in mat.chips()}
    try:
        excess: Optional[float] = float(source.get("excess") or "")
        excess = excess if 0 <= excess <= 10000 else None
    except ValueError:
        excess = None
    return Selection(
        ids=ids[:MAX_POLICIES], state=state, family=family,
        scope="+".join(titles) if family in ("family", "single_parent", "dependants_only") else "",
        wanted=[k for k in items("features") if k in feats.FEATURES],
        chips={c for c in items("chips") if c in chip_keys},
        show_all=source.get("all") == "1", excess=excess)


@bp.route("/card")
def card():
    sel = _selection(request.args)
    if isinstance(sel, str):
        return render_template("_card.html", error=sel), 400
    if not sel.ids:
        return render_template("_card.html", error="Pick at least one policy to compare.")
    try:
        db = Snapshot()
        data = bn.fetch(db, ids=sel.ids, state=sel.state, family=sel.family, scope=sel.scope)
    except EngineError as exc:
        return render_template("_card.html", error=str(exc)), 400

    by_id = {p["id"]: p for p in data["policies"]}
    columns = [i for i in sel.ids if i in by_id and by_id[i]["is_navy"]] + \
              [i for i in sel.ids if i in by_id and not by_id[i]["is_navy"]]
    entries = cat.catalog(db)
    vals = bn.values(data, excess=sel.excess, order=columns)
    anchor = bn.anchor_excess(data, sel.excess, columns)
    navy_ids = [i for i in columns if by_id[i]["is_navy"]]
    comp_ids = [i for i in columns if not by_id[i]["is_navy"]]
    cards = [df.card(n, c, vals, keys=sel.wanted or None, chips=sel.chips)
             for n in navy_ids for c in comp_ids][:MAX_CARDS]
    rows = df.matrix(columns, vals, keys=sel.wanted or None, show_all=sel.show_all)
    chip_rows = {r.key for r in rows if r.tags & sel.chips}
    closest: dict[int, list[dict]] = {}
    for m in data["closest"]:
        if m["comp_id"] in comp_ids and m["navy_id"] not in navy_ids:
            closest.setdefault(m["comp_id"], []).append(m)
    missing_rates = [by_id[i]["name"] for i in columns if "price" not in vals.get(i, {})]
    # The excess levels the chosen policies offer this household: the rep can match premiums at any of them.
    excess_options = sorted({float(r["excess"] or 0) for r in data["rates"]})

    heads = []
    for i in columns:
        e = entries.get(i)
        heads.append({"id": i, "name": by_id[i]["name"], "fund": by_id[i]["fund_name"], "is_navy": by_id[i]["is_navy"],
                      "type": by_id[i]["product_type"], "brand": e.brand if e else "",
                      "tier": cat.TIER_LABELS.get((e.tier if e else "") or "", "")})
    return render_template(
        "_card.html", error=None, heads=heads, by_id=by_id, cards=cards, rows=rows, closest=closest,
        state=sel.state, family=sel.family, scope=sel.scope, anchor=anchor, chips=sel.chips,
        show_all=sel.show_all, wanted=sel.wanted, snapshot=data["snapshot"],
        release_date=data.get("release_date"), missing_rates=missing_rates, chip_rows=chip_rows,
        excess_options=excess_options, chip_labels={c["key"]: c["label"] for c in mat.chips()})


@bp.route("/script", methods=["POST"])
def script():
    """The talking point for one card: two to four sentences the rep can say, from the card's facts
    only, checked by the verifier before they are shown."""
    _check_csrf()
    sel = _selection(request.form)
    if isinstance(sel, str):
        return jsonify({"error": sel}), 400
    try:
        navy_id, comp_id = int(request.form.get("navy", "")), int(request.form.get("comp", ""))
    except ValueError:
        return jsonify({"error": "Which card?"}), 400
    try:
        db = Snapshot()
        data = bn.fetch(db, ids=[navy_id, comp_id], state=sel.state, family=sel.family, scope=sel.scope)
    except EngineError as exc:
        return jsonify({"error": str(exc)}), 400
    by_id = {p["id"]: p for p in data["policies"]}
    if navy_id not in by_id or comp_id not in by_id or not by_id[navy_id]["is_navy"] or by_id[comp_id]["is_navy"]:
        return jsonify({"error": "A talking point needs a Navy Health policy and a competitor's."}), 400
    vals = bn.values(data, excess=sel.excess, order=[navy_id, comp_id])
    the_card = df.card(navy_id, comp_id, vals, keys=sel.wanted or None, chips=sel.chips)
    fact_rows = talk.facts(the_card, navy_name=by_id[navy_id]["name"], comp_name=by_id[comp_id]["name"],
                           snapshot=data["snapshot"], state=sel.state, family=sel.family)
    if not fact_rows:
        return jsonify({"text": "There is nothing on these benefits to talk about.", "outcome": "empty"})

    accounting, refused = calls.begin("script", {"navy": navy_id, "comp": comp_id, "facts": len(fact_rows)})
    if refused:
        return refused
    try:
        pack = EvidencePack.of([Fact.model_validate(f) for f in fact_rows], name="card")
        needs = [chip["label"] for chip in mat.chips() if chip["key"] in sel.chips]
        result = compose(pack, client=get_client(), system=WRITER_PROMPT.read_text(encoding="utf-8"),
                         question="", context={"member_needs": needs, "household": sel.family,
                                               "state": sel.state})
    except Exception as exc:  # the card is still on screen; the rep loses only the talking point
        log.exception("talking point failed")
        calls.end(accounting, WRITER_MODEL, error=exc)
        return jsonify({"error": "The talking point isn't available right now; use the card."}), 502
    calls.end(accounting, WRITER_MODEL, usage=result.usage, extra={"outcome": result.outcome})
    return jsonify({"text": result.text, "outcome": result.outcome})
