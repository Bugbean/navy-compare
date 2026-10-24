"""A read-only window onto the live snapshot, for the question engine.

The engine runs its SQL through psql, the same client the ingest uses, so it
works on every machine the app runs on (see ingest/load.py for why). Each
query runs inside a READ ONLY transaction and returns its rows as JSON, which
avoids parsing psql's text output.

Parameters are never interpolated from free text. Every value a question
supplies is first checked against the enumerations in config/reference.py by
the question layer; lit() then writes it as a SQL literal. The question layer
is the only caller.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any, Mapping, Optional

_PARAM = re.compile(r"(?<![:\w]):([a-z_][a-z0-9_]*)\b")
SNAPSHOT_CACHE_SECONDS = 60
_SNAPSHOT_CACHE: dict[str, tuple[float, int]] = {}


class EngineError(RuntimeError):
    pass


def lit(value: Any) -> str:
    """A value as a SQL literal. Only ever given values the question layer has
    already checked; strings are quoted with '' doubling."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise EngineError(f"no SQL literal for {type(value).__name__}")


def bind(sql: str, params: Mapping[str, Any]) -> str:
    """Substitute :name placeholders. A name with no value is an error, not a NULL."""
    missing: list[str] = []

    def sub(match: re.Match) -> str:
        name = match.group(1)
        if name not in params:
            missing.append(name)
            return match.group(0)
        return lit(params[name])

    out = _PARAM.sub(sub, sql)
    if missing:
        raise EngineError(f"query uses {sorted(set(missing))} but none was given")
    return out


class Snapshot:
    """Reads against one database. `rows` returns a list of dicts."""

    def __init__(self, psql: Optional[str] = None, url: Optional[str] = None) -> None:
        self.psql = psql or os.environ.get("PSQL", "psql")
        self.url = url or os.environ.get("DATABASE_URL", "")
        if not self.url:
            raise EngineError("DATABASE_URL is not set")
        self.env = dict(os.environ)

    def rows(self, sql: str, params: Optional[Mapping[str, Any]] = None) -> list[dict]:
        body = bind(sql.strip().rstrip(";"), params or {})
        script = (
            "BEGIN READ ONLY;\n"
            f"SELECT coalesce(json_agg(t), '[]'::json)::text FROM ({body}) t;\n"
            "COMMIT;\n"
        )
        proc = subprocess.run(
            [self.psql, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-d", self.url, "-f", "-"],
            input=script, capture_output=True, text=True, encoding="utf-8", env=self.env,
        )
        if proc.returncode != 0:
            raise EngineError(proc.stderr.strip() or "psql failed")
        # json_agg breaks its array over several lines, and -q silences the BEGIN and COMMIT tags,
        # so the whole of stdout is the one JSON document.
        return json.loads(proc.stdout.strip() or "[]")

    def live_snapshot_id(self) -> int:
        """The live snapshot. Cached for a minute per database: it changes once a month, and every
        request needs it, so asking each time would add a round trip to every comparison."""
        now = time.monotonic()
        hit = _SNAPSHOT_CACHE.get(self.url)
        if hit and now - hit[0] < SNAPSHOT_CACHE_SECONDS:
            return hit[1]
        found = self.rows("SELECT id FROM snapshots WHERE status = 'live'")
        if len(found) != 1:
            raise EngineError(f"expected one live snapshot, found {len(found)}")
        _SNAPSHOT_CACHE[self.url] = (now, int(found[0]["id"]))
        return int(found[0]["id"])
