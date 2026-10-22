"""alembic upgrade head, as a job: the only way migrations reach a database.

    python -m jobs.run_migrate

Hand-applied migrations get missed in production. Here the deploy runs this
job before the new web revision takes traffic.
"""
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(cfg, "head")
    return 0


if __name__ == "__main__":
    sys.exit(main())
