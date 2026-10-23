"""Load one government PHOL release (privatehealth.gov.au, data.gov.au extract).

    python -m jobs.ingest --release "<dir with the XML and CSV files>" --date 2026-10-01

Stages the files to CSV (with the file-level gates), then loads them into the
database named by DATABASE_URL (see ingest/load.py for the sequence and what
a failure leaves behind). Needs psql on PATH, or pass --psql.

--stage-only writes the CSVs and stops, for a first look at a release
without touching a database.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

from ingest.load import Db, LoadError, load_release
from ingest.stage import ReleaseError, stage_release, write_manifest

# Staged CSVs are large (about 200 MB per release) and rebuildable, so they go to
# the temp directory rather than into a synced folder.
WORK_ROOT = os.path.join(tempfile.gettempdir(), "navy-compare-ingest")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", required=True, help="directory holding the release's files")
    parser.add_argument("--date", required=True, help="release date, YYYY-MM-DD")
    parser.add_argument("--work-dir", help="where the staged CSVs go (default: temp dir/navy-compare-ingest/<date>)")
    parser.add_argument("--reuse-stage", action="store_true", help="skip staging if the CSVs are already there")
    parser.add_argument("--stage-only", action="store_true", help="stage the CSVs and stop")
    parser.add_argument("--psql", default=os.environ.get("PSQL", "psql"))
    args = parser.parse_args(argv)

    work = args.work_dir or os.path.join(WORK_ROOT, args.date)
    manifest_path = os.path.join(work, "manifest.json")
    started = time.time()

    try:
        if args.reuse_stage and os.path.exists(manifest_path):
            print(f"reusing staged CSVs in {work}")
        else:
            print(f"staging {args.release} -> {work}")
            rel = stage_release(args.release, work, args.date)
            write_manifest(rel, work)
            print(json.dumps(rel.counts, indent=1))
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        if args.stage_only:
            print(f"staged in {time.time() - started:.0f}s; not loaded (--stage-only)")
            return 0

        url = os.environ.get("DATABASE_URL")
        if not url:
            print("DATABASE_URL is not set", file=sys.stderr)
            return 2
        db = Db(args.psql, url)
        result = load_release(db=db, stage_dir=work, manifest=manifest)
    except (ReleaseError, LoadError) as e:
        print(f"release not loaded: {e}", file=sys.stderr)
        return 1

    print(result.verification)
    print(f"snapshot {result.snapshot_id} ({result.release_date}) loaded in {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
