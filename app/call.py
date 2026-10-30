"""The call screen: one bar the rep types into, and the comparison that answers it.

    GET  /call             the screen
    GET  /call/index.json  everything the browser needs to understand typing instantly: policies, funds,
                           aliases, benefits, and the words for states, households and needs (compressed)
    GET  /search           server-side type-ahead, the fallback when the index has not loaded (JSON)
    GET  /search/benefits  benefit type-ahead ("crown", "physio", "hips") (JSON)
    POST /call/plan        the model's reading of a request or follow-up, for what the browser could not
                           recognise on its own (JSON)
    GET  /card             the comparison (an HTML fragment)
    POST /script           a talking point for one card (JSON)
    POST /script/stream    the same, streamed as it is written, then replaced by the checked text (SSE)
    POST /script/objection          a reply to one thing the member said, from the same card facts (JSON)
    POST /script/objection/stream   the same, streamed
    POST /call/estimate    what each policy of a card pays back in a year for a usage profile (JSON)
    POST /call/usage       the rep's description of how the household uses extras, read into a usage
                           profile the rep can edit (JSON)

Most of what a rep types is recognised in the browser in milliseconds (static/call.js, against the
index). The model runs alongside for what is left, and never overrides what the rep has confirmed.
Navy Health is always the base: a competitor on its own is compared with its closest Navy Health
policy, chosen automatically and shown so the rep can switch it.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from flask import Blueprint, Response, jsonify, render_template, request, stream_with_context

from ai import calls
from ai.client import ROUTER_MODEL, WRITER_MODEL, get_client
from ai.compose import compose
from ai.facts import EvidencePack, Fact
from ai.usage import read_usage
from app.auth.routes import _check_csrf, csrf_token
from config import features as feats
from config import language as lang
from config.reference import DEPENDANT_TITLES, EXTRAS_ITEMS, FAMILY_TYPES, STATES
from engine import benefits as bn
from engine import catalog as cat
from engine import claims as cl
from engine import differences as df
from engine import estimate as est
from engine import materiality as mat
from engine import plan as planning
from engine import script as talk
from engine import suggest
from engine.db import EngineError, Snapshot

bp = Blueprint("call", __name__)
PROMPT_DIR = Path(__file__).resolve().parent.parent / "ai" / "prompts"
WRITER_PROMPT = PROMPT_DIR / "writer_v1.md"
OBJECTION_PROMPT = PROMPT_DIR / "objection_v1.md"
log = logging.getLogger("app.call")

# The two things a member pushes back with often enough to prepare for ahead of time, worded as what
# the member said so the writer replies to it directly rather than restarting the pitch. "price" is
# only ever offered when the competitor really is cheaper (engine/suggest.follow_ups already guards
# this the same way); "losing" only when the card actually has something in the competitor's favour.
OBJECTION_PRESETS = {
    "price": "The member says the competitor's policy is cheaper. Acknowledge the premium gap honestly, "
             "then answer why it may still be worth it: the strongest navy_better fact available, "
             "ideally one scoped matches_what_member_cares_about, not just the price fact restated.",
    "losing": "The member is worried about what they would lose by switching: what does their current "
              "policy cover, or cover better, that Navy Health does not? Answer honestly from the facts "
              "scoped comp_better - do not answer with what Navy Health covers instead; that is the "
              "opposite of what was asked.",
}
MAX_OBJECTION_CHARS = 300

MAX_POLICIES = 10
MAX_CARDS = 6
EXTRAS_ROWS_SHOWN = 8   # the rest of the extras services are one click away
HOSPITAL_ITEMS_SHOWN = 8  # conditions covered differently; those covered the same are one click away too
MAX_USAGE_CHARS = 400
MAX_REQUEST_CHARS = 500
REAL_STATES = [s for s in STATES if s != "ALL"]
TYPE_CODES = {"hospital": "h", "extras": "e", "combined": "c"}


def _warm_index() -> None:
    """Build the index in the background while the screen loads, so the first keystroke never waits."""
    try:
        cat.catalog(Snapshot())
    except Exception:  # the index route reports any real problem when it is asked
        log.warning("type-ahead index could not be built in advance", exc_info=True)


@bp.route("/call")
def call_screen():
    threading.Thread(target=_warm_index, daemon=True).start()
    return render_template("call.html", csrf=csrf_token(), states=REAL_STATES, families=FAMILY_TYPES,
                           chips=mat.chips(), extras_items=_extras_items())


def _extras_items() -> list[dict]:
    """Every billed extras item, in the order members claim, with its typical fee: the menu the
    estimator's usage lines are chosen from."""
    fees = est.typical_fees()
    order = {s: i for i, s in enumerate(cl.SERVICE_ORDER)}
    items = sorted(EXTRAS_ITEMS.items(), key=lambda kv: (order[kv[1][0]], list(EXTRAS_ITEMS).index(kv[0])))
    return [{"item": k, "label": label, "svc": svc, "service": feats.SERVICE_LABELS.get(svc, svc),
             "fee": (fees.get(k) or {}).get("fee")} for k, (svc, label) in items]


# What each clinical category actually covers, in plain English, keyed the way a Difference names it
# ("cover:BoneJointMuscle") so the card template can look a row's definition up directly.
CAT_DEFS = {f"cover:{k}": v for k, v in feats.CATEGORY_DEFINITIONS.items()}

_CATEGORY_GROUPS = {"Clinical categories", "Extras services", "Extras limits"}
# "annual limit" is in every one of the 27 "Extras limits" labels, so on their own these two words
# would set the focus to all 27 services at once rather than naming any benefit.
_TOO_GENERIC = {"annual", "limit"}


def _benefit_words() -> dict[str, list[str]]:
    """The single words a rep might use for a benefit, so the browser can recognise them without a
    model: every clinical category and extras service's own plain name ("bone", "joint", "muscle" for
    Bone, joint and muscle), not only the ones someone thought to add an everyday synonym for
    ("crown", "physio", config/features.SYNONYMS — merged into FEATURES[key].words already).

    Individual billed items (item: keys, "physiotherapy, first visit") are deliberately left out here:
    their labels share generic words ("first", "visit", "follow-up") across dozens of unrelated
    services, which would make a single word like "first" set the focus to all of them at once. An
    item is only reached through its own deliberate synonym ("crown", "root canal")."""
    out: dict[str, list[str]] = {}
    for key, feature in feats.FEATURES.items():
        if feature.group not in _CATEGORY_GROUPS:
            continue
        for word in feature.words:
            if len(word) >= 3 and word not in lang.STOP_WORDS and word not in _TOO_GENERIC:
                out.setdefault(word, [])
                if key not in out[word]:
                    out[word].append(key)
    for phrase, keys in feats.SYNONYMS.items():
        item_keys = [k for k in keys if k.startswith("item:")]
        if not item_keys:
            continue
        for word in phrase.lower().split():
            if len(word) >= 3 and word not in lang.STOP_WORDS:
                out.setdefault(word, [])
                out[word] += [k for k in item_keys if k not in out[word]]
    return out


@bp.route("/call/index.json")
def call_index():
    try:
        c = cat.catalog(Snapshot())
    except EngineError as exc:
        return jsonify({"error": str(exc)}), 503
    payload = {
        "snapshot": c.snapshot_id,
        # One short row per policy: id, name, fund, type, tier, navy, brand, corporate, excess options.
        "policies": [[p.id, p.name, p.fund_code, TYPE_CODES[p.product_type], cat.TIER_LABELS.get(p.tier or "", ""),
                      1 if p.is_navy else 0, p.brand, 1 if p.corporate else 0,
                      [int(x) if x == int(x) else x for x in p.excesses]] for p in c.policies],
        "funds": {f.code: {"name": f.name, "brands": list(f.brands), "aliases": c.aliases.get(f.code, []),
                           "restricted": f.restricted, "navy": f.is_navy, "count": f.count} for f in c.funds},
        "features": [[k, f.label, f.group, sorted(f.words)] for k, f in feats.FEATURES.items()],
        "chips": mat.chips(),
        "chip_tags": mat.load().get("tags", {}),
        "benefit_words": _benefit_words(),
        "states": lang.STATE_WORDS,
        "households": lang.HOUSEHOLD_WORDS,
        "needs": lang.NEED_WORDS,
        "stop": sorted(lang.STOP_WORDS),
        "navy_markers": sorted(lang.NAVY_MARKERS),
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    etag = '"' + hashlib.sha1(body).hexdigest()[:20] + '"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag})
    headers = {"ETag": etag, "Cache-Control": "private, max-age=300", "Vary": "Accept-Encoding"}
    if "gzip" in request.headers.get("Accept-Encoding", ""):
        body = gzip.compress(body, compresslevel=6)
        headers["Content-Encoding"] = "gzip"
    return Response(body, mimetype="application/json", headers=headers)


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


def _context() -> Optional[dict]:
    """The comparison so far, as the browser describes it, trimmed to what the model needs."""
    raw = request.form.get("context") or ""
    if not raw:
        return None
    try:
        ctx = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(ctx, dict):
        return None
    keep = {"navy": ctx.get("navy"), "competitors": ctx.get("competitors"), "state": ctx.get("state"),
            "household": ctx.get("household"), "benefits": ctx.get("benefits"), "needs": ctx.get("needs")}
    return {k: v for k, v in keep.items() if v}


@bp.route("/call/plan", methods=["POST"])
def call_plan():
    _check_csrf()
    text = (request.form.get("request") or "").strip()
    if not text:
        return jsonify({"error": "Type what you want to compare."}), 400
    if len(text) > MAX_REQUEST_CHARS:
        return jsonify({"error": f"Keep it under {MAX_REQUEST_CHARS} characters."}), 400
    context = _context()
    accounting, refused = calls.begin("plan", {"chars": len(text), "follow_up": bool(context)})
    if refused:
        return refused
    try:
        p = planning.plan(text, client=get_client(), model=ROUTER_MODEL, context=context)
        offer = planning.suggestions(cat.catalog(Snapshot()), p)
    except Exception as exc:  # the rep gets a plain message; the activity log keeps the detail
        log.exception("plan failed")
        calls.end(accounting, ROUTER_MODEL, error=exc)
        return jsonify({"error": "I couldn't read that. Try naming a policy or a company."}), 502
    calls.end(accounting, ROUTER_MODEL, usage=p.usage,
              extra={"intent": p.intent, "features": p.features, "navy_terms": len(p.navy_terms),
                     "comp_terms": len(p.comp_terms)})
    return jsonify({
        "understood": bool(p.navy_terms or p.comp_terms or p.navy_all or p.features or p.intent == "cheaper")
                      and p.confidence >= 0.4,
        "intent": p.intent, "navy": offer["navy"], "comp": offer["comp"],
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
    focus: list[str]
    chips: set[str]
    show_all: bool
    excess: Optional[float]
    auto_navy: bool


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
    if family in ("family", "single_parent", "dependants_only") and not titles:
        titles = ["Child", "NonClassified", "Student"]
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
        focus=[k for k in items("focus") if k in feats.FEATURES],
        chips={c for c in items("chips") if c in chip_keys},
        show_all=source.get("all") == "1", excess=excess, auto_navy=source.get("auto") != "0")


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
        by_id = {p["id"]: p for p in data["policies"]}
        navy_ids = [i for i in sel.ids if i in by_id and by_id[i]["is_navy"]]
        comp_ids = [i for i in sel.ids if i in by_id and not by_id[i]["is_navy"]]
        # Navy Health is always the base. A competitor on its own is compared with its closest Navy
        # Health policy, which the rep sees named as the best match and can switch.
        auto = None
        if sel.auto_navy and comp_ids and not navy_ids:
            best = suggest.closest_alternatives(data["closest"], comp_ids[0])
            if best:
                auto = best[0]
                navy_ids = [auto["id"]]
                data = bn.fetch(db, ids=navy_ids + comp_ids, state=sel.state, family=sel.family, scope=sel.scope)
                by_id = {p["id"]: p for p in data["policies"]}
    except EngineError as exc:
        return render_template("_card.html", error=str(exc)), 400

    columns = navy_ids + comp_ids
    entries = cat.catalog(db)
    vals = bn.values(data, excess=sel.excess, order=columns)
    anchor = bn.anchor_excess(data, sel.excess, columns)
    cards = [df.card(n, c, vals, keys=sel.wanted or None, chips=sel.chips)
             for n in navy_ids for c in comp_ids][:MAX_CARDS]
    rows = df.matrix(columns, vals, keys=sel.wanted or None, show_all=sel.show_all)

    alternatives = suggest.closest_alternatives(data["closest"], comp_ids[0], exclude=set(navy_ids)) if comp_ids else []
    cheaper: list[dict] = []
    if cards and cards[0].price and cards[0].price.direction == df.COMP_BETTER:
        try:
            cheaper = suggest.cheaper_navy(db, navy_id=cards[0].navy_id, comp_id=cards[0].comp_id,
                                           product_type=by_id[cards[0].navy_id]["product_type"],
                                           state=sel.state, family=sel.family, scope=sel.scope)
        except EngineError:
            cheaper = []
    # The call sheet: every extras service with its limit, what a visit pays and the gap, in the order
    # members claim (a service the rep asked about first, and opened); the hospital conditions only one
    # side covers; the price a month and a year. A focused benefit with no service (price, a clinical
    # category, a waiting period) is also answered on its own line at the top.
    focus_services = cl.services_in(sel.focus)
    focus_categories = set(cl.categories_in(sel.focus))
    focus_other = [k for k in sel.focus if not feats.item_service(k)]
    shown = []
    for c in cards:
        answers = [df.compare_pair(k, vals.get(c.navy_id, {}).get(k), vals.get(c.comp_id, {}).get(k))
                   for k in focus_other if vals.get(c.navy_id, {}).get(k) or vals.get(c.comp_id, {}).get(k)]
        extras, neither = cl.extras_sheet(c.navy_id, c.comp_id, vals, focus=focus_services)
        # Only offer to prepare for an objection the card can actually speak to: "theirs is cheaper"
        # only when it really is, "what am I losing" only when something is genuinely in their favour.
        objections = []
        if c.price and c.price.direction == df.COMP_BETTER:
            objections.append("price")
        if any(d.key != "price" for d in c.watch):
            objections.append("losing")
        shown.append({"card": c, "answers": answers, "extras": extras, "neither": neither,
                      "hospital": cl.hospital_sheet(c.navy_id, c.comp_id, vals, focus=focus_categories),
                      "price": cl.price_gap(c.price), "objections": objections})
    followups = suggest.follow_ups(cards[0], active_chips=sel.chips, cheaper=cheaper, focus=sel.focus) if cards else []

    heads = []
    for i in columns:
        e = entries.get(i)
        heads.append({"id": i, "name": by_id[i]["name"], "fund": by_id[i]["fund_name"], "is_navy": by_id[i]["is_navy"],
                      "type": by_id[i]["product_type"], "brand": e.brand if e else "",
                      "tier": cat.TIER_LABELS.get((e.tier if e else "") or "", "")})
    meta = {"columns": columns, "auto_navy": auto,
            "alternatives": alternatives[:3], "cheaper": [{"id": x["id"], "name": x["current_name"],
                                                          "premium": float(x["premium"])} for x in cheaper],
            "pairs": [[c.navy_id, c.comp_id] for c in cards], "anchor": anchor}
    return render_template(
        "_card.html", error=None, heads=heads, by_id=by_id, shown=shown, rows=rows,
        state=sel.state, family=sel.family, scope=sel.scope, anchor=anchor, chips=sel.chips,
        show_all=sel.show_all, wanted=sel.wanted, focus=sel.focus, snapshot=data["snapshot"],
        release_date=data.get("release_date"),
        missing_rates=[by_id[i]["name"] for i in columns if "price" not in vals.get(i, {})],
        chip_rows={r.key for r in rows if r.tags & sel.chips},
        excess_options=sorted({float(r["excess"] or 0) for r in data["rates"]}),
        followups=followups, auto=auto, meta=meta, comp_only=not cards and bool(comp_ids), cat_defs=CAT_DEFS,
        focus_categories=focus_categories, extras_rows_shown=EXTRAS_ROWS_SHOWN,
        hospital_items_shown=HOSPITAL_ITEMS_SHOWN)


def _pair():
    """The Navy Health and competitor policy ids a form names, or an error response."""
    try:
        navy_id, comp_id = int(request.form.get("navy", "")), int(request.form.get("comp", ""))
    except ValueError:
        return None, (jsonify({"error": "Which card?"}), 400)
    return (navy_id, comp_id), None


def _estimate_json(pe: est.PolicyEstimate) -> dict:
    return {"total": pe.total, "lines": [
        {"item": ln.item, "label": ln.label, "service": feats.SERVICE_LABELS.get(ln.service, ln.service),
         "claims": ln.claims, "fee": ln.fee, "per_claim": ln.per_claim, "paid": ln.paid, "note": ln.note}
        for ln in pe.lines]}


@bp.route("/call/estimate", methods=["POST"])
def call_estimate():
    """What each policy of one card pays back in a year for a usage profile, and the premiums beside
    it. Plain arithmetic on the government data (engine/estimate.py); no model call."""
    _check_csrf()
    sel = _selection(request.form)
    if isinstance(sel, str):
        return jsonify({"error": sel}), 400
    pair, early = _pair()
    if early:
        return early
    navy_id, comp_id = pair
    try:
        uses = est.clean_uses(json.loads(request.form.get("uses") or "[]"))
    except ValueError:
        return jsonify({"error": "That usage list couldn't be read."}), 400
    try:
        data = bn.fetch(Snapshot(), ids=[navy_id, comp_id], state=sel.state, family=sel.family, scope=sel.scope)
    except EngineError as exc:
        return jsonify({"error": str(exc)}), 400
    by_id = {p["id"]: p for p in data["policies"]}
    if navy_id not in by_id or comp_id not in by_id:
        return jsonify({"error": "Which card?"}), 400
    vals = bn.values(data, excess=sel.excess, order=[navy_id, comp_id])
    fees = est.typical_fees()
    out: dict = {"uses": [{"item": u.item, "times": u.times, "people": u.people, "fee": u.fee} for u in uses]}
    for side, pid in (("navy", navy_id), ("comp", comp_id)):
        price = vals.get(pid, {}).get("price")
        out[side] = {"name": by_id[pid]["name"], "fund": by_id[pid]["fund_name"],
                     "premium_year": round(price.size * 12, 2) if price else None,
                     **_estimate_json(est.estimate(data, pid, uses, fees))}
    return jsonify(out)


@bp.route("/call/usage", methods=["POST"])
def call_usage():
    """How the member's household uses extras, read from what the rep typed into a profile the rep can
    edit. The model chooses items and counts only; every dollar comes from /call/estimate."""
    _check_csrf()
    text = (request.form.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Say how they use their extras, e.g. “two check-ups each, glasses, weekly physio”."}), 400
    if len(text) > MAX_USAGE_CHARS:
        return jsonify({"error": f"Keep it under {MAX_USAGE_CHARS} characters."}), 400
    family = request.form.get("family", "")
    household = family.replace("_", " ") if family in FAMILY_TYPES else ""
    accounting, refused = calls.begin("usage", {"chars": len(text)})
    if refused:
        return refused
    try:
        read = read_usage(text, client=get_client(), model=ROUTER_MODEL, household=household)
    except Exception as exc:  # the rep can still add lines by hand
        log.exception("usage failed")
        calls.end(accounting, ROUTER_MODEL, error=exc)
        return jsonify({"error": "I couldn't read that. Add the lines by hand below."}), 502
    calls.end(accounting, ROUTER_MODEL, usage=read.usage, extra={"lines": len(read.uses)})
    return jsonify({"uses": [{"item": u.item, "times": u.times, "people": u.people} for u in read.uses],
                    "unclear": read.unclear})


def _talk_pack(sel: "Selection", navy_id: int, comp_id: int):
    """The facts for one card's talking point, or a reason it cannot be written."""
    db = Snapshot()
    data = bn.fetch(db, ids=[navy_id, comp_id], state=sel.state, family=sel.family, scope=sel.scope)
    by_id = {p["id"]: p for p in data["policies"]}
    if navy_id not in by_id or comp_id not in by_id or not by_id[navy_id]["is_navy"] or by_id[comp_id]["is_navy"]:
        return None, "A talking point needs a Navy Health policy and a competitor's."
    vals = bn.values(data, excess=sel.excess, order=[navy_id, comp_id])
    the_card = df.card(navy_id, comp_id, vals, keys=sel.wanted or None, chips=sel.chips)
    rows = talk.facts(the_card, navy_name=by_id[navy_id]["name"], comp_name=by_id[comp_id]["name"],
                      snapshot=data["snapshot"], state=sel.state, family=sel.family)
    if not rows:
        return None, "There is nothing on these benefits to talk about."
    return EvidencePack.of([Fact.model_validate(f) for f in rows], name="card"), None


def _prepared(question: str):
    """sel, the two policy ids, the card's facts and the context the writer is given — shared by the
    opening talking point (question="") and a reply to something the member said (question=<what they
    said>). Both read from exactly the same facts; only what is asked of them differs."""
    _check_csrf()
    sel = _selection(request.form)
    if isinstance(sel, str):
        return None, (jsonify({"error": sel}), 400)
    try:
        navy_id, comp_id = int(request.form.get("navy", "")), int(request.form.get("comp", ""))
    except ValueError:
        return None, (jsonify({"error": "Which card?"}), 400)
    try:
        pack, why = _talk_pack(sel, navy_id, comp_id)
    except EngineError as exc:
        return None, (jsonify({"error": str(exc)}), 400)
    if pack is None:
        return None, (jsonify({"text": why, "outcome": "empty"}), 200)
    needs = [chip["label"] for chip in mat.chips() if chip["key"] in sel.chips]
    context = {"member_needs": needs, "household": sel.family, "state": sel.state}
    return (sel, navy_id, comp_id, pack, context, question), None


def _talk_request():
    return _prepared("")


def _objection_request():
    _check_csrf()
    preset = request.form.get("preset", "")
    free = (request.form.get("objection") or "").strip()[:MAX_OBJECTION_CHARS]
    question = OBJECTION_PRESETS.get(preset) or free
    if not question:
        return None, (jsonify({"error": "What did the member say?"}), 400)
    return _prepared(question)


def _compose_once(prepared, system_path: Path, kind: str):
    sel, navy_id, comp_id, pack, context, question = prepared
    accounting, refused = calls.begin(kind, {"navy": navy_id, "comp": comp_id, "facts": len(pack.facts)})
    if refused:
        return refused
    try:
        result = compose(pack, client=get_client(), system=system_path.read_text(encoding="utf-8"),
                         question=question, context=context)
    except Exception as exc:  # the card is still on screen; the rep loses only this one reply
        log.exception("%s failed", kind)
        calls.end(accounting, WRITER_MODEL, error=exc)
        return jsonify({"error": "That isn't available right now; use the card."}), 502
    calls.end(accounting, WRITER_MODEL, usage=result.usage, extra={"outcome": result.outcome})
    payload = {"text": result.text, "outcome": result.outcome}
    if question:
        payload["question"] = question
    return jsonify(payload)


@bp.route("/script", methods=["POST"])
def script():
    """The talking point for one card: two to four sentences from the card's facts only, checked by the
    verifier before they are shown."""
    prepared, early = _talk_request()
    if early:
        return early
    return _compose_once(prepared, WRITER_PROMPT, "script")


@bp.route("/script/objection", methods=["POST"])
def script_objection():
    """A reply to one specific thing the member just said — a preset ("they say it's cheaper") or
    whatever the rep types — from the same facts and the same checks as the talking point, aimed at
    that one objection instead of an opening pitch."""
    prepared, early = _objection_request()
    if early:
        return early
    return _compose_once(prepared, OBJECTION_PROMPT, "objection")


def _sse(event: dict) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


def _compose_stream(prepared, system_path: Path, kind: str):
    """Streamed version of `_compose_once`: each `preview` is unchecked prose shown as such; the closing
    `done` event carries the checked text (or the facts, if the check failed), which replaces it. A rep
    is never shown an unchecked sentence as if it were checked."""
    sel, navy_id, comp_id, pack, context, question = prepared
    accounting, refused = calls.begin(kind, {"navy": navy_id, "comp": comp_id, "facts": len(pack.facts),
                                             "stream": True})
    if refused:
        return refused
    events: "queue.Queue[dict]" = queue.Queue()
    system = system_path.read_text(encoding="utf-8")
    client = get_client()

    def write() -> None:
        try:
            result = compose(pack, client=client, system=system, question=question, context=context,
                             on_event=events.put)
            events.put({"type": "_done", "result": result})
        except Exception as exc:  # reported to the page as an error event below
            events.put({"type": "_error", "error": exc})

    threading.Thread(target=write, daemon=True).start()

    def stream():
        while True:
            try:
                ev = events.get(timeout=45)
            except queue.Empty:
                calls.end(accounting, WRITER_MODEL, error=TimeoutError(f"{kind} timed out"))
                yield _sse({"type": "error", "error": "That took too long; use the card."})
                return
            ev_kind = ev.get("type")
            if ev_kind == "answer_preview":
                yield _sse({"type": "preview", "text": ev.get("text", "")})
            elif ev_kind == "_done":
                result = ev["result"]
                calls.end(accounting, WRITER_MODEL, usage=result.usage, extra={"outcome": result.outcome})
                payload = {"type": "done", "text": result.text, "outcome": result.outcome}
                if question:
                    payload["question"] = question
                yield _sse(payload)
                return
            elif ev_kind == "_error":
                log.error("%s failed", kind, exc_info=ev["error"])
                calls.end(accounting, WRITER_MODEL, error=ev["error"])
                yield _sse({"type": "error", "error": "That isn't available right now; use the card."})
                return

    return Response(stream_with_context(stream()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.route("/script/stream", methods=["POST"])
def script_stream():
    """The talking point as it is written, streamed."""
    prepared, early = _talk_request()
    if early:
        return early
    return _compose_stream(prepared, WRITER_PROMPT, "script")


@bp.route("/script/objection/stream", methods=["POST"])
def script_objection_stream():
    """A reply to what the member said, streamed."""
    prepared, early = _objection_request()
    if early:
        return early
    return _compose_stream(prepared, OBJECTION_PROMPT, "objection")
