# The database: where it runs and how migrations reach it

Supabase (Postgres, Auth, Storage), one project per environment, both in
Sydney (`ap-southeast-2`), created fresh for this engagement and sharing
nothing with any other project:

| Environment | Supabase project | Plan | Who applies migrations |
| --- | --- | --- | --- |
| `local` | none — Postgres 16 on this machine | — | you, by hand (below) |
| `staging` | `navy-compare-staging` | Free or Pro | first apply by hand (below); then the Cloud Run migrate job on every deploy to `main` |
| `prod` | `navy-compare-prod` | Pro (daily backups, 7-day PITR, no pausing) | the Cloud Run migrate job, on a tag |

Migrations are raw SQL under `migrations/versions/`, run by Alembic. The
job is `python -m jobs.run_migrate` (`alembic upgrade head`); nothing is
ever applied to staging or prod by typing SQL into the dashboard.

## Creating a Supabase project

In the Supabase dashboard: **New project** → organisation of your choice,
name `navy-compare-staging`, region **Sydney (ap-southeast-2)**, generate a
database password and keep it. Then from the project:

- **Connect → Session pooler** (port 5432; user
  `postgres.<project-ref>`, host `aws-0-ap-southeast-2.pooler.supabase.com`):
  this is `DATABASE_URL`. Session mode behaves like a direct connection, so
  DDL is fine. Do not use the transaction pooler on 6543 — DDL through it is
  unreliable. The direct host `db.<project-ref>.supabase.co` is IPv6-only
  (Supabase charges for IPv4 on it) and most home networks and Cloud Run
  cannot reach it; the pooler has IPv4 addresses.
- **Project Settings → API**: the project URL is `SUPABASE_URL`, the
  `service_role` key is `SUPABASE_SERVICE_ROLE_KEY`.

Put them in `.env` (copied from `.env.example`; `.env` is gitignored and
must stay out of git).

New Supabase projects run Postgres 17. The migrations are standard SQL and
were verified on 16; nothing in them is version-specific.

## Applying migrations the first time

`jobs/run_migrate.py` needs psycopg, which needs a native `libpq`. On a
Windows-on-ARM machine there is no ARM64 libpq for Python, so from such a
machine the first apply uses Alembic's offline mode — the same migrations
rendered to one SQL file — and `psql`:

```
PYTHONUTF8=1 DATABASE_URL="postgresql://offline@localhost/offline" \
  python -m alembic upgrade head --sql > migrate_up.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrate_up.sql
```

The offline SQL includes Alembic's own `alembic_version` bookkeeping, so a
later `run_migrate` on Cloud Run sees the right revision and applies only
what is new. `PYTHONUTF8=1` matters on Windows: the migrations' comments
contain characters the console's default encoding cannot write.

On a Linux or Mac machine with libpq (`brew install libpq`,
`apt install libpq5`), just run `python -m jobs.run_migrate` with
`DATABASE_URL` set.

## Verifying what was applied

```
python scripts/db_check.py         # needs psycopg
```

or, with psql only, the queries it runs: `alembic_version`, every public
table with its row count and RLS state, the functions, and — the one that
matters on Supabase — any grant to `anon` or `authenticated`, which must be
**none**. Supabase grants every new public table, sequence and function to
those roles by default; every migration revokes them for everything it
creates, including `alembic_version`, and this check is how you know that
held.

Extensions are the one thing the migrations cannot police: Supabase
installs them as `supabase_admin` with its own grants to `anon` and
`authenticated`, which the `postgres` role cannot revoke. So every
extension goes `WITH SCHEMA extensions` (where Supabase keeps its own
pgcrypto), a schema PostgREST does not expose, and the audit covers
`public`. Staging was verified this way on 2026-10-23: 31 tables all with
RLS, no grants to anon/authenticated/PUBLIC in public, pg_trgm in
`extensions`.

## Local Postgres without Docker

`docker compose up -d db` is the intended local path. Without Docker, EDB's
PostgreSQL binaries zip (`postgresql-16.x-windows-x64-binaries.zip`; the x64
build runs under emulation on ARM) gives `initdb`, `pg_ctl` and `psql`:

```
initdb -D pgdata -U postgres --auth=trust -E UTF8 --locale=C
pg_ctl -D pgdata -o "-p 5433 -c listen_addresses=127.0.0.1" -l pg.log -w start
psql -h 127.0.0.1 -p 5433 -U postgres -c "CREATE DATABASE navy_compare"
psql -h 127.0.0.1 -p 5433 -U postgres -d navy_compare -f docker/initdb/00_supabase_shim.sql
psql -h 127.0.0.1 -p 5433 -U postgres -d navy_compare -v ON_ERROR_STOP=1 -f migrate_up.sql
```

The shim creates the `auth` schema, `auth.users`, `auth.uid()` and the
`anon` / `authenticated` / `service_role` roles that Supabase provides, so
the migrations run unchanged. To test the Supabase grant posture locally,
add the default privileges Supabase sets before migrating:

```sql
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES    TO anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO anon, authenticated;
```

and confirm the grant check above still returns none.

## Rolling back

`alembic downgrade <to> --sql` renders the reverse. Every migration's
`DOWNGRADE` has been applied and re-upgraded on Postgres 16 (0002 → base →
0002). Note that Alembic's offline downgrade leaves the empty
`alembic_version` table in place; drop it before re-applying an offline
upgrade, or use online mode, which handles it.
