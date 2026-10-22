"""Creating an account. Accounts are invite only; this is the one way in.

From the admin Users page (auth.admin_invite), or for the very first admin,
from a shell with the service role key in the environment:

    python -m app.auth.invite admin you@example.com "Your Name"

The account is created confirmed and approved, with a random password nobody
sees. The person gets in through the welcome email (from the Users page) or,
from the shell, a password reset email.
"""
from __future__ import annotations

import secrets
import sys
import time
from datetime import datetime, timezone

from app import auth
from app.db import get_sb

ROLES = ("rep", "lead", "admin")


def modules_for(sb, role: str) -> list[str]:
    """A rep holds the rep modules; a lead holds those and the lead ones; an
    admin holds everything (and is_admin sees every module regardless)."""
    layers = {"rep": ("rep",), "lead": ("rep", "lead"), "admin": ("rep", "lead")}[role]
    return [m["key"] for m in sb.table("modules").select("key,layer").execute().data or []
            if m["layer"] in layers]


def invite(email: str, full_name: str, role: str, *, invited_by: str | None = None) -> str:
    """Make one approved, confirmed account. Returns its id. Raises
    auth.AuthError if Supabase refuses (an address already in use, say)."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    sb = get_sb()
    # A password nobody will ever use or see: the welcome link is how they get
    # in, and it replaces this. Generated rather than left blank because
    # GoTrue wants one, and random so it cannot be guessed in the window
    # before they choose their own.
    created = auth._gotrue("POST", "/admin/users", {
        "email": email,
        "password": secrets.token_urlsafe(32),
        "email_confirm": True,          # an admin typing it is the confirmation
        "user_metadata": {"full_name": full_name},
    })
    uid = created.get("id")
    if not uid:
        raise auth.AuthError(f"no id came back for {email}", 500)

    # The profile row is made by the on_auth_user_created trigger. It arrives
    # 'pending'; an invited account goes straight to approved.
    for _ in range(10):
        if sb.table("user_profiles").select("id").eq("id", uid).execute().data:
            break
        time.sleep(0.3)
    sb.table("user_profiles").update({
        "full_name": full_name, "status": "approved", "is_admin": role == "admin",
        "decided_at": datetime.now(timezone.utc).isoformat(), "decided_by": invited_by,
    }).eq("id", uid).execute()

    mods = modules_for(sb, role)
    if mods:
        sb.table("user_module_access").insert(
            [{"user_id": uid, "module_key": m, "granted_by": invited_by} for m in mods]).execute()
    auth.forget_user(uid)
    return uid


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] not in ROLES:
        print(__doc__)
        return 2
    role, email, name = argv[0], argv[1].strip().lower(), argv[2].strip()
    uid = invite(email, name, role)
    from app.auth.routes import PUBLIC_BASE_URL
    auth.send_password_reset(email, f"{PUBLIC_BASE_URL}/auth/callback")
    print(f"Created {role} {email} ({uid}). A password email is on its way.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
