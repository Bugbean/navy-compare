"""Tests do not touch staging or production.

On the platform this came from they did: tests set credentials with
`os.environ.setdefault`, which keeps a real value if one is already there, so
on any machine with .env loaded the suite wrote 256 rows into the production
audit log before anyone noticed.

So the Supabase credentials are replaced for every test, before any test
module is imported, and DATABASE_URL must point at a local database. A test
that genuinely needs a real project asks for it explicitly:

    TEST_USE_REAL_DB=1 python -m pytest tests/

Which should be rare, and never in CI.
"""
import os
from urllib.parse import urlparse

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "db", "postgres"}

# Before collection, so a module-level create_client() in any test file gets
# the unusable values rather than the real ones.
if os.environ.get("TEST_USE_REAL_DB") != "1":
    db_host = urlparse(os.environ.get("DATABASE_URL", "")).hostname
    if db_host and db_host not in LOCAL_HOSTS:
        raise SystemExit(f"Refusing to run tests: DATABASE_URL points at {db_host}, "
                         "not a local database. Unset it or point it at Docker Compose.")
    os.environ["SUPABASE_URL"] = "https://tests.invalid"
    os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "tests-do-not-reach-a-database"
    # Anything that sends or spends, held shut for the same reason. A test
    # that emails somebody is a test that emailed somebody.
    for key in ("RESEND_API_KEY", "ANTHROPIC_API_KEY"):
        os.environ.pop(key, None)
    os.environ.setdefault("FLASK_SECRET_KEY", "tests-only")
