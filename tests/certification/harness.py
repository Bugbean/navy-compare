"""Scoring a certification run: each item's answer against its expected route, facts and plainness.

A live run (the runner arrives with the router and writer, week 3) writes one JSON record per item per
repeat:

    {"id": "C001", "repeat": 1, "question_type": "is_covered", "slots": {...},
     "cited": ["F2"], "verifier_result": "pass", "text": "...", "exception": null}

classify_run() scores one record; classify_item() combines an item's repeats: PASS only if every repeat
passes, FLAKY if some do (the first thing to fix with a prompt change), FAIL otherwise. The report
lists each failure with the routed type, the facts cited and the verifier's decision, so a failure is
diagnosable in a minute. Nothing here is a substitute for the sales lead reading every answer.
"""
from __future__ import annotations

import collections
from pathlib import Path
from typing import Any

import yaml

from ai.readability import fk_grade, max_sentence_words

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "tests" / "certification" / "corpus.yaml"
QUESTION_TYPES = ROOT / "config" / "question_types.yaml"

SOURCES = ("navy_health", "recording", "written")
CATEGORIES = ("PASS", "FLAKY", "FAIL", "NOT_RUN")


def load_corpus(path: Path = CORPUS) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("items") or []


def load_question_types(path: Path = QUESTION_TYPES) -> dict[str, dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("types") or {}


def _norm(v: Any) -> str:
    return " ".join(str(v).casefold().split())


def classify_run(item: dict, run: dict) -> tuple[bool, list[str]]:
    """(passed, why not) for one repeat of one item."""
    why: list[str] = []
    if run.get("exception"):
        return False, [f"exception: {str(run['exception'])[:200]}"]
    if run.get("question_type") != item["question_type"]:
        why.append(f"routed to {run.get('question_type')!r}, expected {item['question_type']!r}")
    got_slots = run.get("slots") or {}
    for slot, want in (item.get("slots") or {}).items():
        if _norm(got_slots.get(slot, "")) != _norm(want):
            why.append(f"slot {slot}: got {got_slots.get(slot)!r}, expected {want!r}")
    cited = set(run.get("cited") or [])
    missing = [f for f in item.get("must_cite") or [] if f not in cited]
    if missing:
        why.append(f"did not cite {', '.join(missing)}")
    wrong = [f for f in item.get("must_not_cite") or [] if f in cited]
    if wrong:
        why.append(f"cited {', '.join(wrong)}, which make the answer wrong")
    if item["question_type"] != "none" and run.get("verifier_result") not in ("pass", "partial"):
        why.append(f"verifier result {run.get('verifier_result')!r}")
    text = run.get("text") or ""
    plain = item.get("plainness") or {}
    if text and plain.get("max_grade") is not None and fk_grade(text) > plain["max_grade"]:
        why.append(f"reading grade {fk_grade(text)} over {plain['max_grade']}")
    if text and plain.get("max_words") is not None and max_sentence_words(text) > plain["max_words"]:
        why.append(f"a sentence of {max_sentence_words(text)} words, over {plain['max_words']}")
    return not why, why


def classify_item(item: dict, runs: list[dict]) -> tuple[str, list[str]]:
    if not runs:
        return "NOT_RUN", ["no run recorded"]
    results = [classify_run(item, r) for r in runs]
    passed = sum(ok for ok, _ in results)
    notes = [f"repeat {r.get('repeat', i + 1)}: " + "; ".join(why) for i, (r, (ok, why)) in enumerate(zip(runs, results, strict=True)) if not ok]
    if passed == len(runs):
        return "PASS", notes
    return ("FLAKY" if passed else "FAIL"), notes


def report(corpus: list[dict], runs: list[dict]) -> tuple[list[dict], str]:
    """(classified items, a markdown summary)."""
    by_id: dict[str, list[dict]] = collections.defaultdict(list)
    for r in runs:
        by_id[r.get("id")].append(r)
    classified = []
    for item in corpus:
        cat, notes = classify_item(item, by_id.get(item["id"], []))
        classified.append({**item, "category": cat, "notes": notes, "runs": by_id.get(item["id"], [])})
    counts = collections.Counter(c["category"] for c in classified)
    scored = sum(counts[k] for k in ("PASS", "FLAKY", "FAIL"))
    rate = f"{100 * counts['PASS'] / scored:.1f}%" if scored else "n/a"
    lines = ["# Certification run", "", f"Pass rate: **{rate}** ({counts['PASS']} of {scored} scored items; "
             "an item passes only if every repeat passes)", ""]
    lines += [f"- {k}: {counts.get(k, 0)}" for k in CATEGORIES]
    for cat in ("FAIL", "FLAKY"):
        rows = [c for c in classified if c["category"] == cat]
        if rows:
            lines += ["", f"## {cat}", ""]
            for c in rows:
                lines.append(f"- **{c['id']}** ({c['question_type']}) {c['question']}")
                lines += [f"  - {n}" for n in c["notes"]]
    return classified, "\n".join(lines) + "\n"
