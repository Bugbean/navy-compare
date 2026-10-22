"""Alembic environment. Migrations are raw SQL (op.execute); there is no ORM
metadata to autogenerate from.

DATABASE_URL is the direct connection (port 5432), not the Supavisor pooler:
DDL in transaction-mode pooling is unreliable. Applied by `python -m
jobs.run_migrate` as a deploy step, never by hand.
"""
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

if context.config.config_file_name:
    fileConfig(context.config.config_file_name)


def _url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    # SQLAlchemy needs the driver named; psycopg 3 is the one installed.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
