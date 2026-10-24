"""Local testing without sign-in: NAVY_DEV_NO_LOGIN=1.

Off by default, and it refuses to switch on anywhere but a laptop: the app must be
served from localhost, and it must not be running on Cloud Run (which sets K_SERVICE).
Anywhere else the setting is an error at start-up, not a quiet way in.

The stand-in account exists only in this process, so the database refuses its activity-log rows
(they reference a user that is not there). The page works the same; only the local activity and AI
usage records are missing.
"""
from __future__ import annotations

import logging
import os

from app import auth

log = logging.getLogger("app.devmode")
LOCAL_PREFIXES = ("http://localhost", "http://127.0.0.1")


def bypass_enabled() -> bool:
    if os.environ.get("NAVY_DEV_NO_LOGIN") != "1":
        return False
    base = os.environ.get("PUBLIC_BASE_URL", "")
    if os.environ.get("K_SERVICE") or not base.startswith(LOCAL_PREFIXES):
        raise RuntimeError("NAVY_DEV_NO_LOGIN=1 is only allowed with PUBLIC_BASE_URL on localhost "
                           "and outside Cloud Run. Remove it for any deployed environment.")
    return True


def dev_user() -> auth.User:
    """The stand-in account: approved, an admin, every module."""
    return auth.User(id="00000000-0000-0000-0000-000000000000", email="local-dev@localhost", full_name="Local developer",
                     phone=None, status="approved", is_admin=True)
