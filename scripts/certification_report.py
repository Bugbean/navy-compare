#!/usr/bin/env python
"""Score a live certification run against the corpus and write the report.

    python scripts/certification_report.py --runs review/cert_run.jsonl --out review/cert_report.md

--runs is one JSON record per line (see tests/certification/harness.py for the shape).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.certification.harness import load_corpus, report  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--classified-out")
    args = ap.parse_args()
    runs = [json.loads(line) for line in Path(args.runs).read_text(encoding="utf-8").splitlines() if line.strip()]
    classified, md = report(load_corpus(), runs)
    Path(args.out).write_text(md, encoding="utf-8")
    if args.classified_out:
        Path(args.classified_out).write_text(json.dumps(classified, indent=1, default=str), encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
