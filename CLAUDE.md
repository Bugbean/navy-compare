# Navy Health Policy Compare

The build plan is `../Navy Health Policy Compare Development Plan.md`. Read the section for the work at hand before starting it.

## Stack

Python 3.11, Flask 3 (`app/`, factory in `app/__init__.py`, entry `wsgi.py`), Jinja + htmx, no JS build. Supabase (Sydney) for Postgres, Auth and Storage. Migrations are raw SQL in Alembic (`migrations/versions/`), applied only by `python -m jobs.run_migrate`, never by hand. Deploy is Cloud Run via `deploy/`.

## Commands

- Tests: `python -m pytest`
- Lint: `ruff check .`
- Local DB: `docker compose up -d db`, then `DATABASE_URL=postgresql://postgres:postgres@localhost:5432/navy_compare python -m jobs.run_migrate`
- First admin: `python -m app.auth.invite admin <email> "<name>"` (needs Supabase env)

## Rules

- Access fails closed: every new route prefix goes in `ROUTE_MODULES` in `app/__init__.py`, or it is refused.
- No inline `<script>` in templates (CSP). JS goes in `app/static/`.
- No personal member data beyond what the plan's Personal data section allows.
- Tests never reach staging or prod (`tests/conftest.py` enforces it).

## Porting from nbl-platform

Some code here comes from Chris's NBL platform (`../../NBL Commentary/nbl-platform`, read only); `docs/PROVENANCE.md` lists every file. Everything the plan reuses has been brought across, so nothing further is planned. If something more is ever needed:

1. Export from a pinned commit, never the working copy: `git -C <nbl-platform> show <commit>:<path>`.
2. Rename `nbl_` prefixes and remove anything about players, teams, games, seasons, the live feed or commentary.
3. Bring its tests across and make them pass.
4. Add a row to `docs/PROVENANCE.md`: source path, commit, target, lines, what changed.
5. `tests/test_no_nbl_refs.py` must stay green. It scans every file except `docs/PROVENANCE.md` and this one.
