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

## Reused code

Some of this code was reused from an earlier Bugbean platform; `docs/PROVENANCE.md` lists every file, where it came from and what changed. Nothing further is planned from it. `tests/test_clean_source.py` must stay green: it fails if that platform's names or domain turn up anywhere except `docs/PROVENANCE.md`.
