"""Load one government release into a database.

The load goes through psql rather than a Python driver. psql is the one
client that runs on every machine this has to run on: psycopg has no ARM64
Windows build, and the same psql reads the same SQL files on a laptop, in
Docker and in the Cloud Run migrate job. Every step is a plain statement or
file, so a release can be re-run and inspected by hand.

Sequence for one release:
  1. Refuse a release date that is already live, unless it is the same release.
  2. Insert the snapshot row (status downloading).
  3. TRUNCATE the staging tables and \\copy the staged CSVs in.
  4. Mark the snapshot staged, with its counts.
  5. Run promote.sql then derive.sql in one transaction. Any gate failure
     rolls the whole thing back and leaves the previous snapshot live.
  6. On failure, mark the snapshot rejected, so the attempt stays inspectable.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from urllib.parse import urlparse

SQL_DIR = os.path.join(os.path.dirname(__file__), "sql")
STAGING_TABLES = ("stg_products", "stg_funds", "stg_agreement_hospitals", "stg_definitions", "stg_limit_groups")


class LoadError(RuntimeError):
    pass


def _sql_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


class Db:
    """A database reached through psql. The URL is never printed."""

    def __init__(self, psql: str, url: str) -> None:
        self.psql = psql
        self.url = url
        host = urlparse(url).hostname or ""
        self.env = dict(os.environ)
        if host not in ("localhost", "127.0.0.1", "::1"):
            # Supabase and Cloud SQL both require TLS on the public address.
            self.env.setdefault("PGSSLMODE", "require")

    def _run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            # -d, not a bare argument: psql stops reading options at the first bare one.
            [self.psql, "-X", "-v", "ON_ERROR_STOP=1", "-d", self.url, *args],
            capture_output=True, text=True, encoding="utf-8", env=self.env,
        )
        if check and proc.returncode != 0:
            raise LoadError(proc.stderr.strip() or proc.stdout.strip() or f"psql exited {proc.returncode}")
        return proc

    def rows(self, sql: str) -> list[list[str]]:
        out = self._run(["-At", "-F", "\t", "-c", sql]).stdout
        return [line.split("\t") for line in out.splitlines() if line != ""]

    def execute(self, sql: str) -> None:
        self._run(["-q", "-c", sql])

    def copy_csv(self, table: str, columns: tuple[str, ...], path: str) -> None:
        """COPY one staged CSV. The statement timeout is lifted in the same
        session: a 200 MB upload over a pooled connection outlasts the role's
        default, and a timeout here would reject a release that is fine."""
        cols = ", ".join(columns)
        forward = path.replace("\\", "/")
        script = (
            "SET statement_timeout = 0;\n"
            f"\\copy {table} ({cols}) FROM '{forward}' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8')\n"
        )
        script_path = os.path.join(os.path.dirname(path), f"copy_{table}.sql")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)
        self._run(["-q", "-f", script_path])

    def run_files(self, files: list[str], variables: dict[str, object]) -> str:
        """Run SQL files in one transaction, in order. Returns psql's output so
        the checks in verify.sql can be shown."""
        args = ["-1", "-q"]
        for k, v in variables.items():
            args += ["-v", f"{k}={v}"]
        for f in files:
            args += ["-f", f]
        return self._run(args).stdout


@dataclass
class LoadResult:
    snapshot_id: int
    release_date: str
    counts: dict
    verification: str


def load_release(*, db: Db, stage_dir: str, manifest: dict) -> LoadResult:
    """Load a staged release. stage_dir holds the CSVs stage.stage_release wrote,
    and manifest is the parsed manifest.json beside them."""
    release_date = manifest["release_date"]
    sha = manifest["sha256"]

    live = db.rows("SELECT id, release_date::text, source_hash FROM snapshots "
                   "WHERE status = 'live' ORDER BY release_date DESC")
    if live and release_date < live[0][1]:
        raise LoadError(f"{release_date} is older than the live release {live[0][1]}; load newer releases only")
    same = db.rows(f"SELECT id, status, source_hash FROM snapshots WHERE release_date = {_sql_literal(release_date)}")
    for sid, status, src_hash in same:
        if status == "live" and src_hash == sha:
            return LoadResult(int(sid), release_date, {}, "already live, nothing to do")
        if status in ("live", "superseded"):
            raise LoadError(f"release {release_date} is already {status} with different content")
        db.execute(f"DELETE FROM snapshots WHERE id = {sid}")  # a rejected or half-run attempt: no children yet

    prev_id = int(live[0][0]) if live else 0
    sid = int(db.rows(
        "INSERT INTO snapshots (release_date, source_url, source_hash, status) "
        f"VALUES ({_sql_literal(release_date)}, {_sql_literal('local release directory')}, {_sql_literal(sha)}, 'downloading') "
        "RETURNING id")[0][0])

    try:
        db.execute("TRUNCATE " + ", ".join(STAGING_TABLES))
        for table, file_name in (
            ("stg_funds", "stg_funds.csv"),
            ("stg_agreement_hospitals", "stg_agreement_hospitals.csv"),
            ("stg_definitions", "stg_definitions.csv"),
            ("stg_limit_groups", "stg_limit_groups.csv"),
            ("stg_products", "stg_products.csv"),
        ):
            db.copy_csv(table, _columns(table), os.path.join(stage_dir, file_name))

        counts = {"counts": manifest["counts"], "change_counts": manifest["change_counts"],
                  "expected": manifest["expected"], "sha256": sha, "file_names": manifest["file_names"]}
        db.execute(f"UPDATE snapshots SET status = 'staged', record_counts = {_sql_literal(json.dumps(counts))}::jsonb "
                   f"WHERE id = {sid}")

        db.run_files(
            [os.path.join(SQL_DIR, "promote.sql"), os.path.join(SQL_DIR, "derive.sql")],
            {
                "snapshot_id": sid,
                "release_date": release_date,
                "prev_snapshot_id": prev_id,
                "expected_products": sum(v for k, v in manifest["expected"].items() if k != "funds"),
                "expected_funds": manifest["expected"]["funds"],
            },
        )
    except Exception:
        db.execute(f"UPDATE snapshots SET status = 'rejected' WHERE id = {sid}")
        raise

    report = db.run_files([os.path.join(SQL_DIR, "verify.sql")], {"snapshot_id": sid})
    return LoadResult(sid, release_date, counts, report)


def _columns(table: str) -> tuple[str, ...]:
    return {
        "stg_funds": ("fund_code", "fund_item_id", "raw"),
        "stg_agreement_hospitals": ("row_no", "provider_code", "raw"),
        "stg_definitions": ("def_id", "kind", "body"),
        "stg_limit_groups": ("lg_id", "body"),
        "stg_products": ("gov_product_item_id", "gov_product_id", "gov_product_code", "fund_code",
                         "product_type", "status", "name", "state", "premium_month", "source_file", "raw"),
    }[table]
