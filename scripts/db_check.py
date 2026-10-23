#!/usr/bin/env python
"""What the database actually holds: migration version, tables with row
counts, the resolver functions, and RLS state. Reads DATABASE_URL.

    python scripts/db_check.py

Used after a migrate against local Postgres or Supabase staging to see that
what was applied is what the migration says, and after an ingest to see the
counts land where docs/DATA_MODEL.md says they should.
"""
from __future__ import annotations

import os
import sys

import psycopg


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    with psycopg.connect(url, connect_timeout=15) as conn, conn.cursor() as cur:
        cur.execute("SELECT version()")
        print(cur.fetchone()[0])
        cur.execute("SELECT version_num FROM alembic_version")
        rows = cur.fetchall()
        print("alembic_version:", [r[0] for r in rows] or "(none)")

        cur.execute("""
            SELECT c.relname, c.relrowsecurity,
                   (SELECT count(*) FROM pg_policies p WHERE p.tablename = c.relname) AS policies
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY c.relname""")
        tables = cur.fetchall()
        print(f"\n{'table':34} {'rows':>9}  rls  policies")
        for name, rls, policies in tables:
            cur.execute(f'SELECT count(*) FROM "{name}"')
            n = cur.fetchone()[0]
            print(f"{name:34} {n:9}  {'on ' if rls else 'OFF'}  {policies}")

        cur.execute("""
            SELECT p.proname, pg_get_function_identity_arguments(p.oid)
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public' ORDER BY p.proname""")
        print("\nfunctions:")
        for name, args in cur.fetchall():
            print(f"  {name}({args})")

        # The check that matters on Supabase, which grants every new public
        # table, sequence and function to these roles by default. Must be none.
        cur.execute("""
            SELECT 'table', grantee, table_name FROM information_schema.role_table_grants
             WHERE table_schema = 'public' AND grantee IN ('anon', 'authenticated')
            UNION ALL
            SELECT 'sequence', grantee, object_name FROM information_schema.role_usage_grants
             WHERE object_schema = 'public' AND object_type = 'SEQUENCE' AND grantee IN ('anon', 'authenticated')
            UNION ALL
            SELECT 'function', grantee, routine_name FROM information_schema.role_routine_grants
             WHERE routine_schema = 'public' AND grantee IN ('anon', 'authenticated', 'PUBLIC')
            ORDER BY 1, 2, 3""")
        leaks = cur.fetchall()
        print("\nanon/authenticated/PUBLIC grants in public:", "none (correct)" if not leaks else "")
        for kind, grantee, name in leaks:
            print(f"  LEAK {kind} {name} -> {grantee}")
    return 1 if leaks else 0


if __name__ == "__main__":
    sys.exit(main())
