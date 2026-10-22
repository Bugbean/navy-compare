"""
Account, sign-in and user-admin routes.

The session itself is two Supabase tokens in cookies (see app.auth). Flask's
own session holds only the CSRF token.

Accounts are invite only: an admin invites someone from the Users page (or
`python -m app.auth.invite` for the first admin), and the welcome email is how
they choose a password. There is no self registration.

Email links use Supabase's token_hash flow, which lands server-side on
/auth/callback. The Supabase email templates must point there — see
deploy/email-templates/.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
from datetime import datetime, timezone

from flask import Blueprint, abort, g, redirect, render_template, request, session, url_for
from markupsafe import escape

from app import auth

log = logging.getLogger(__name__)

bp = Blueprint("auth", __name__)

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/")

# The account that owns the platform. No other admin may suspend it, reject
# it, take its admin rights, set its password or move its email address —
# every one of which ends in the same place, the owner locked out of their own
# platform, and the last two hand the account to whoever did it.
#
# Admins are trusted people; this is not about suspecting them. It is that an
# account which can be taken over by any of several people is only as safe as
# the least careful of them, and there is no way back from losing the only
# account that can restore the others. The owner can still do all of it to
# their own account.
#
# Comma-separated, so a second owner can be added without a deploy.
OWNER_EMAILS = {e.strip().lower() for e in
                os.environ.get("OWNER_EMAILS", "chris.eade@bugbean.com.au").split(",") if e.strip()}


def _is_owner(email: str | None) -> bool:
    return bool(email) and email.strip().lower() in OWNER_EMAILS


def _may_touch_owner(actor, target_email: str | None) -> bool:
    """Whether this admin may make a destructive change to this account.

    Anyone may act on a non-owner. Only the owner may act on the owner.
    """
    if not _is_owner(target_email):
        return True
    return _is_owner(getattr(actor, "email", None))


_MAIL_FROM = os.environ.get("MAIL_FROM", "")      # empty: mailer.DEFAULT_FROM

# Eight, and nothing about which characters. Composition rules ("must contain
# a symbol") push people towards one predictable substitution and a sticky
# note, which is the opposite of what they are for; NIST dropped them in
# SP 800-63B for that reason. Length is the part that matters and the part
# people can actually meet. Supabase itself accepts six and imposes no
# character rule (probed Sep 2026), so this is the only gate.
MIN_PASSWORD_LENGTH = 8


# ── Helpers ───────────────────────────────────────────────────────────────────

# Absolute URLs `next` may point at. Empty by default: only same-site paths.
TRUSTED_REDIRECT_HOSTS = {
    h.strip().lower() for h in
    os.environ.get("TRUSTED_REDIRECT_HOSTS", "").split(",")
    if h.strip()
}


def safe_next(path: str | None) -> str | None:
    """Where it's safe to send someone after sign-in.

    `next` comes straight from the query string, so an unvalidated redirect
    would let /login?next=https://evil.example send someone who just typed
    their real password anywhere an attacker likes. Allowed: a same-site
    relative path, or an https URL whose host is exactly one of ours.
    """
    if not path:
        return None
    if path.startswith("/") and not path.startswith("//") and "\\" not in path:
        return path
    from urllib.parse import urlparse
    try:
        u = urlparse(path)
    except ValueError:
        return None
    # Exact host match — no suffix matching, which evil-example.com or
    # example.com.evil.example would otherwise slip through.
    if u.scheme == "https" and (u.hostname or "").lower() in TRUSTED_REDIRECT_HOSTS \
            and not u.username and not u.password:
        return path
    return None


def _callback_url() -> str:
    return f"{PUBLIC_BASE_URL}/auth/callback"


def set_session(resp, sess: auth.Session):
    resp.set_cookie(auth.COOKIE_ACCESS, sess.access_token,
                    **auth.cookie_kwargs(max_age=sess.expires_in))
    # Refresh token outlives the access token; Supabase rotates it on use.
    resp.set_cookie(auth.COOKIE_REFRESH, sess.refresh_token,
                    **auth.cookie_kwargs(max_age=60 * 60 * 24 * 30))
    return resp


def clear_session(resp):
    for name in (auth.COOKIE_ACCESS, auth.COOKIE_REFRESH):
        kw = auth.cookie_kwargs()
        kw.pop("max_age", None)
        resp.delete_cookie(name, path=kw["path"], domain=kw.get("domain"))
    return resp


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def _check_csrf():
    sent = request.form.get("csrf", "")
    if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
        abort(400, "Form expired — reload the page and try again.")


def _sb():
    from app.db import get_sb
    return get_sb()


def _send_email(to: list[str], subject: str, html: str, *,
                cc: list[str] | None = None, bcc: list[str] | None = None,
                reply_to: str | None = None) -> bool:
    """Send via Resend (app.auth.mailer)."""
    from app.auth import mailer
    return mailer.send(to, subject, html, sender=_MAIL_FROM or None, cc=cc, bcc=bcc,
                       reply_to=reply_to)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_CURRENT = object()


def log_activity(category: str, event: str, *, user=_CURRENT, email: str | None = None,
                 status: int | None = None, detail: dict | None = None,
                 background: bool = False, **ai) -> int | None:
    """One activity_log row for the current request.

    user defaults to whoever is signed in; pass user=None for events with no
    account behind them yet (a failed sign-in records the address tried, via
    email=). Never raises.
    """
    if user is _CURRENT:
        user = getattr(g, "user", None)
    return auth.log_event(
        service="web", category=category, event=event, user=user, email=email,
        path=request.path, method=request.method, status=status,
        ip=auth.client_ip(request.headers.get("X-Forwarded-For"), request.remote_addr),
        user_agent=request.headers.get("User-Agent"), detail=detail,
        background=background, **ai)


_EMAIL_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _email_html(heading: str, body_html: str, button: str | None = None,
                url: str | None = None, note_html: str = "") -> str:
    """The app's emails in the same design as the Supabase templates in
    deploy/email-templates/, so every message reads as one product.

    Table layout and inline styles throughout — email clients ignore
    <style> blocks, flexbox and grid. body_html / note_html are inserted as
    markup, so callers must escape anything a user typed.
    """
    f = _EMAIL_FONT
    btn = ""
    if button and url:
        btn = f"""<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin-top:6px;"><tr>
          <td align="center" bgcolor="#1f4e8c" style="border-radius:24px;">
            <a href="{url}" target="_blank" style="display:inline-block;padding:13px 30px;font-family:{f};font-size:15px;font-weight:700;color:#ffffff;text-decoration:none;border-radius:24px;">{button}</a>
          </td></tr></table>"""
    note = (f'<p style="margin:0;font-size:13px;line-height:1.6;color:#777777;">{note_html}</p>'
            if note_html else "")
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"></head>
<body style="margin:0;padding:0;background:#eef0f3;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#eef0f3;">
  <tr><td align="center" style="padding:32px 16px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;background:#ffffff;border-radius:10px;overflow:hidden;">
      <tr><td style="background:#1f4e8c;height:4px;line-height:4px;font-size:0;">&nbsp;</td></tr>
      <tr><td style="padding:36px 40px 30px;font-family:{f};color:#1a1a1a;">
        <p style="margin:0 0 6px;font-size:12px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:#1f4e8c;">Navy Health Policy Compare</p>
        <h1 style="margin:0 0 14px;font-size:22px;line-height:1.3;font-weight:700;color:#1a1a1a;">{heading}</h1>
        <div style="margin:0 0 22px;font-size:15px;line-height:1.6;color:#444444;">{body_html}</div>
        {btn}
        <div style="margin-top:22px;">{note}</div>
      </td></tr>
      <tr><td style="background:#f6f7f9;padding:18px 40px;font-family:{f};font-size:12px;line-height:1.6;color:#888888;border-top:1px solid #e6e8ec;">
        Navy Health Policy Compare<br>
        <a href="{PUBLIC_BASE_URL}" style="color:#888888;">{PUBLIC_BASE_URL.replace('https://', '')}</a>
      </td></tr>
    </table>
  </td></tr>
</table></body></html>"""


def _user_or_none(user_id: str | None):
    """Profile for a user id, for attributing a log row. None if it can't be
    loaded; the row still records the email."""
    if not user_id:
        return None
    try:
        return auth.load_user(user_id)
    except Exception:
        return None


# ── Sign-in ───────────────────────────────────────────────────────────────────

@bp.route("/login", methods=["GET", "POST"])
def login():
    next_path = safe_next(request.args.get("next"))
    error = notice = None
    email = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if request.form.get("mode") == "magic":
            auth.send_magic_link(email, _callback_url())
            log_activity("auth", "magic_link_requested", user=None, email=email)
            # Same message whether or not the account exists — no enumeration.
            notice = f"If {email} has an account, a sign-in link is on its way."
        else:
            try:
                sess = auth.sign_in_password(email, request.form.get("password", ""))
                log_activity("auth", "sign_in", user=_user_or_none(sess.user_id), email=email,
                             detail={"method": "password"})
                resp = redirect(next_path or url_for("home"))
                return set_session(resp, sess)
            except auth.AuthError as e:
                error = "Email or password is incorrect."
                # The reason Supabase gave (wrong password, unconfirmed email,
                # its own rate limit) goes in the log, never on the page: the
                # page stays identical either way so it can't be used to test
                # which addresses have accounts.
                log_activity("auth", "sign_in_failed", user=None, email=email, status=401,
                             detail={"method": "password", "reason": e.message})
    return render_template("auth/login.html", email=email, error=error,
                           notice=notice, next_path=next_path)


@bp.route("/logout", methods=["GET", "POST"])
def logout():
    if getattr(g, "user", None) is not None:
        log_activity("auth", "sign_out")
    auth.sign_out(request.cookies.get(auth.COOKIE_ACCESS))
    session.clear()
    return clear_session(redirect(url_for("auth.login")))


# ── Email-link landing ────────────────────────────────────────────────────────

_LINK_TYPES = ("signup", "email", "magiclink", "recovery", "invite")

@bp.route("/auth/callback", methods=["GET", "POST"])
def callback():
    """Emailed sign-in links land here. Opening one (GET) only shows a
    Continue button; the one-time link is used when the person presses it
    (POST). Mail systems that open every link to scan it (Microsoft 365
    does) would otherwise spend the link before the person clicked, so they
    saw "expired"; scanners open links but don't press buttons."""
    src = request.form if request.method == "POST" else request.args
    token_hash = src.get("token_hash", "")
    link_type = src.get("type", "")
    if not token_hash or link_type not in _LINK_TYPES:
        return render_template("auth/message.html", title="Link not valid",
            body="This link is incomplete or has expired. Request a new one from the sign-in page.")
    if request.method == "GET":
        return render_template("auth/continue.html", token_hash=token_hash, link_type=link_type,
                               csrf=csrf_token())
    _check_csrf()
    try:
        sess = auth.verify_email_link(token_hash, link_type)
    except auth.AuthError as e:
        log_activity("auth", "email_link_failed", user=None, status=e.status,
                     detail={"type": link_type, "reason": e.message})
        return render_template("auth/message.html", title="Link expired",
            body="This link has already been used or has expired. Request a new one from the sign-in page.")

    if link_type == "recovery":
        log_activity("auth", "password_reset_link_used", user=_user_or_none(sess.user_id))
        return set_session(redirect(url_for("auth.reset_password")), sess)

    user = auth.load_user(sess.user_id, use_cache=False)
    if user:
        log_activity("auth", "sign_in", user=user, detail={"method": link_type})

    dest = url_for("home") if user and user.approved else url_for("auth.pending")
    return set_session(redirect(dest), sess)


def _welcome_account(token: str):
    """(user_id, profile) if this welcome link is the account's current one
    and not yet spent, else (None, None)."""
    uid, _, nonce = (token or "").partition(".")
    if not uid or not nonce or len(uid) != 36:
        return None, None
    try:
        meta = (auth.admin_get_user(uid).get("user_metadata") or {})
    except auth.AuthError:
        return None, None
    current = meta.get("welcome_nonce") or ""
    if not current or not hmac.compare_digest(current, nonce):
        return None, None
    rows = _sb().table("user_profiles").select("email,full_name,status").eq("id", uid).execute().data
    if not rows or rows[0]["status"] != "approved":
        return None, None
    return uid, rows[0]


@bp.route("/auth/welcome/<token>", methods=["GET", "POST"])
def welcome(token):
    """The welcome email's link. Showing the form changes nothing, however
    many times it's opened; saving a password spends the link, signs them
    in and lands them on the platform."""
    uid, prof = _welcome_account(token)
    if uid is None:
        return render_template("auth/welcome_again.html", csrf=csrf_token(), sent=False, spent=True)
    error = None
    if request.method == "POST":
        _check_csrf()
        pw = request.form.get("password", "")
        if len(pw) < MIN_PASSWORD_LENGTH:
            error = f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        elif pw != request.form.get("password_confirm", ""):
            error = "Passwords don't match."
        else:
            try:
                auth.admin_update_user(uid, {"password": pw, "user_metadata": {"welcome_nonce": None}})
                sess = auth.sign_in_password(prof["email"], pw)
            except auth.AuthError as e:
                error = e.message
            else:
                user = _user_or_none(uid)
                log_activity("auth", "welcome_link_used", user=user)
                log_activity("auth", "password_changed", user=user, detail={"via": "welcome link"})
                log_activity("auth", "sign_in", user=user, detail={"method": "welcome"})
                auth.forget_user(uid)
                return set_session(redirect(url_for("home")), sess)
    return render_template("auth/reset.html", error=error, min_len=MIN_PASSWORD_LENGTH,
                           welcome=True, csrf=csrf_token(), email=prof["email"])


@bp.route("/auth/welcome-again", methods=["POST"])
def welcome_again():
    """Someone with an old or spent welcome link asks for a fresh one. The
    reply never says whether the address has an account, and each address
    gets at most three in an hour."""
    _check_csrf()
    email = (request.form.get("email") or "").strip().lower()
    if email and "@" in email:
        rows = _sb().table("user_profiles").select("id,status").eq("email", email).execute().data
        if rows and rows[0]["status"] == "approved":
            since = datetime.now(timezone.utc).timestamp() - 3600
            recent = _sb().table("activity_log").select("id").eq("event", "welcome_email_sent") \
                .eq("detail->>target_email", email) \
                .gte("occurred_at", datetime.fromtimestamp(since, timezone.utc).isoformat()).execute().data
            if len(recent) < 3:
                send_welcome(rows[0]["id"], sent_by=None)
            log_activity("auth", "welcome_link_requested", user=None, email=email,
                         detail={"sent": len(recent) < 3})
    return render_template("auth/welcome_again.html", csrf=csrf_token(), sent=True)


# ── Password reset ────────────────────────────────────────────────────────────

@bp.route("/auth/forgot", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        auth.send_password_reset(email, _callback_url())
        log_activity("auth", "password_reset_requested", user=None, email=email)
        return render_template("auth/message.html", title="Check your email",
            body=f"If {escape(email)} has an account, a reset link is on its way.")
    return render_template("auth/forgot.html")


@bp.route("/auth/reset", methods=["GET", "POST"])
def reset_password():
    """Reached from a recovery email via /auth/callback, which has already set
    a session — so the password change is authorised by that session."""
    token = request.cookies.get(auth.COOKIE_ACCESS)
    if not auth.verify_access_token(token):
        return redirect(url_for("auth.forgot_password"))
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        if len(pw) < MIN_PASSWORD_LENGTH:
            error = f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        elif pw != request.form.get("password_confirm", ""):
            error = "Passwords don't match."
        else:
            try:
                auth.update_password(token, pw)
                claims = auth.verify_access_token(token) or {}
                log_activity("auth", "password_changed", user=_user_or_none(claims.get("sub")))
                return redirect(url_for("home"))
            except auth.AuthError as e:
                error = e.message
    return render_template("auth/reset.html", error=error, min_len=MIN_PASSWORD_LENGTH,
                           welcome=False)


@bp.route("/auth/pending")
def pending():
    user = getattr(g, "user", None)
    return render_template("auth/pending.html", user=user)


# ── Admin ─────────────────────────────────────────────────────────────────────

def _require_admin():
    user = getattr(g, "user", None)
    if not (user and user.is_admin):
        abort(403)
    return user


@bp.route("/admin/users")
def admin_users():
    _require_admin()
    status = request.args.get("status", "")
    q = _sb().table("user_profiles").select("*").order("created_at", desc=True)
    if status in auth.STATUSES:
        q = q.eq("status", status)
    users = q.execute().data
    counts = {s: 0 for s in auth.STATUSES}
    for r in _sb().table("user_profiles").select("status").execute().data:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    # When each account was last doing anything. One read of a view rather
    # than a last_seen_at column, which would mean a write on every request
    # for a figure nobody reads more than once a day. An account that has done
    # nothing is simply absent here, and the page leaves its cell empty.
    seen = {}
    try:
        for r in (_sb().table("v_user_last_activity")
                  .select("user_id,last_activity,last_sign_in,events,ai_questions")
                  .execute().data or []):
            seen[r["user_id"]] = r
    except Exception as exc:                # noqa: BLE001
        # The column goes blank for everyone rather than failing the page.
        log.warning("last activity unavailable: %s", str(exc)[:160])

    for u in users:
        u["activity"] = seen.get(u["id"])

    return render_template("admin/users.html", users=users, counts=counts,
                           status=status, csrf=csrf_token())


@bp.route("/admin/users/invite", methods=["POST"])
def admin_invite():
    """Create an approved account and send its welcome email in one step."""
    admin = _require_admin()
    _check_csrf()
    from app.auth.invite import ROLES, invite
    email = (request.form.get("email") or "").strip().lower()[:320]
    full_name = (request.form.get("full_name") or "").strip()[:200]
    role = request.form.get("role", "rep")
    if not _EMAIL_RE.match(email) or role not in ROLES:
        return redirect(url_for("auth.admin_users", invite="email"))
    if _sb().table("user_profiles").select("id").eq("email", email).execute().data:
        return redirect(url_for("auth.admin_users", invite="exists"))
    try:
        uid = invite(email, full_name, role, invited_by=admin.id)
    except auth.AuthError as e:
        return redirect(url_for("auth.admin_users", invite="failed", why=e.message))
    log_activity("admin", "user_invited", detail={"target_id": uid, "target_email": email, "role": role})
    sent, reason = send_welcome(uid)
    return redirect(url_for("auth.admin_user", user_id=uid,
                            welcome="sent" if sent else "failed", why=None if sent else reason))


def send_welcome(user_id: str, *, sent_by=None, replaces_earlier: bool = False) -> tuple[bool, str]:
    """Email an approved account a welcome with a link to choose their
    password (no password is ever emailed). Returns (sent, reason).

    The link is /auth/welcome/<user id>.<secret>, the secret kept in the
    account's Supabase metadata (welcome_nonce). It does NOT expire and
    opening it changes nothing, so a mail system that opens links to scan
    them (Microsoft 365 does, and used up one-time links before anyone
    clicked) can't spend it. It's spent only when the person saves a
    password; sending again replaces it.
    """
    rows = _sb().table("user_profiles").select("email,full_name,status").eq("id", user_id).execute().data
    if not rows:
        return False, "no such account"
    prof = rows[0]
    if prof["status"] != "approved":
        return False, "only approved accounts get a welcome email"
    nonce = secrets.token_urlsafe(24)
    try:
        auth.admin_update_user(user_id, {"user_metadata": {"welcome_nonce": nonce}})
    except auth.AuthError as e:
        return False, f"couldn't make the link: {e.message}"
    link = f"{PUBLIC_BASE_URL}/auth/welcome/{user_id}.{nonce}"
    first = escape(((prof.get("full_name") or "").split() or ["there"])[0])

    sent = _send_email(
        [prof["email"]], "Your Navy Health Policy Compare account",
        _email_html(
            "Welcome to Policy Compare",
            f"""Hi {first}, you've been given an account on Navy Health Policy Compare.
            <br><br>Your sign-in is this email address, <strong>{escape(prof['email'])}</strong>.
            Choose a password to get started.{'<br><br>This replaces our earlier email, whose link stopped working.' if replaces_earlier else ''}""",
            button="Choose your password", url=link,
            note_html=f"""The button keeps working until you've chosen your password. After that,
            sign in at <a href="{PUBLIC_BASE_URL}/login" style="color:#888888;">{PUBLIC_BASE_URL.replace('https://', '')}</a>
            with this address and your password. If you weren't expecting this, you can ignore it."""))
    log_activity("admin", "welcome_email_sent" if sent else "welcome_email_failed",
                 user=sent_by if sent_by is not None else _CURRENT,
                 detail={"target_id": user_id, "target_email": prof["email"]})
    return sent, "sent" if sent else "the email service didn't accept it"


@bp.route("/admin/users/<user_id>/welcome", methods=["POST"])
def admin_user_welcome(user_id):
    _require_admin()
    _check_csrf()
    sent, reason = send_welcome(user_id)
    return redirect(url_for("auth.admin_user", user_id=user_id,
                            welcome="sent" if sent else "failed", why=None if sent else reason))


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_ADMIN_PASSWORD = 8      # matches MIN_PASSWORD_LENGTH; see the note there


@bp.route("/admin/users/<user_id>/account", methods=["POST"])
def admin_user_account(user_id):
    """Change someone's name, phone, sign-in address or password directly.

    Kept apart from the access form above so a typo in a password can't roll
    back somebody's modules, and so the audit trail says which of the two
    happened.
    """
    admin = _require_admin()
    _check_csrf()
    sb = _sb()

    rows = sb.table("user_profiles").select("email,full_name,phone").eq("id", user_id).execute().data
    if not rows:
        abort(404)
    before = rows[0]
    target = {"target_id": user_id, "target_email": before["email"]}

    def back(**kw):
        return redirect(url_for("auth.admin_user", user_id=user_id, **kw))

    full_name = (request.form.get("full_name") or "").strip()[:200]
    phone = (request.form.get("phone") or "").strip()[:40]
    email = (request.form.get("email") or "").strip().lower()[:320]
    pw = request.form.get("new_password") or ""
    pw2 = request.form.get("new_password_confirm") or ""

    wants_password = bool(pw or pw2)
    wants_email = bool(email) and email != (before["email"] or "").lower()
    # Setting a password or moving an email hands the account over, so these
    # are the two that must never be available against the owner.
    if (wants_password or wants_email) and not _may_touch_owner(admin, before["email"]):
        return back(acct="owner")
    if wants_password:
        if pw != pw2:
            return back(acct="mismatch")
        if len(pw) < MIN_ADMIN_PASSWORD:
            return back(acct="short")
    if email and not _EMAIL_RE.match(email):
        return back(acct="email")

    # Supabase Auth first: if it refuses, the profile is left alone rather
    # than drifting out of step with the account people actually sign in to.
    auth_changes: dict = {}
    if wants_password:
        auth_changes["password"] = pw
    if wants_email:
        auth_changes["email"] = email
        auth_changes["email_confirm"] = True      # an admin typing it is the confirmation
    meta = {}
    if full_name != (before.get("full_name") or ""):
        meta["full_name"] = full_name
    if phone != (before.get("phone") or ""):
        meta["phone"] = phone
    if meta:
        auth_changes["user_metadata"] = meta
    if not auth_changes:
        return back(saved=1)
    try:
        auth.admin_update_user(user_id, auth_changes)
    except Exception as exc:                      # noqa: BLE001
        log.warning("admin account change failed for %s: %s", user_id, exc)
        return back(acct="failed")

    profile = {"full_name": full_name, "phone": phone}
    if wants_email:
        profile["email"] = email
    if wants_password or wants_email:
        # Supabase revokes their tokens, but this platform verifies JWTs
        # itself rather than asking Supabase on every request, so an issued
        # token would otherwise keep working until it expired — up to an hour
        # after the change. The cut-off refuses anything issued before now.
        profile["sessions_valid_from"] = _now()
    sb.table("user_profiles").update(profile).eq("id", user_id).execute()

    # What changed, and never the password itself.
    if wants_password:
        log_activity("admin", "password_set", detail=target)
    if wants_email:
        log_activity("admin", "email_changed",
                     detail={**target, "from": before["email"], "to": email})
    if meta.get("full_name") is not None or meta.get("phone") is not None:
        log_activity("admin", "profile_edited", detail={
            **target,
            "from": {"full_name": before.get("full_name"), "phone": before.get("phone")},
            "to": {"full_name": full_name, "phone": phone}})

    auth.forget_user(user_id)
    # Setting a password already revokes their tokens server-side (checked
    # against this Supabase project), so anyone signed in is signed out.
    return back(acct="saved", pw=1 if wants_password else None)


@bp.route("/admin/users/<user_id>", methods=["GET", "POST"])
def admin_user(user_id):
    admin = _require_admin()
    sb = _sb()
    decider = admin.id

    if request.method == "POST":
        _check_csrf()
        before = sb.table("user_profiles") \
                   .select("status,email,full_name,is_admin,ai_limit_minute,ai_limit_hour,ai_limit_day") \
                   .eq("id", user_id).execute().data
        if not before:
            abort(404)
        before = before[0]
        before_modules = {r["module_key"] for r in sb.table("user_module_access")
                          .select("module_key").eq("user_id", user_id).execute().data}
        target = {"target_id": user_id, "target_email": before["email"]}

        # Validate the limits before changing anything, so a typo can't leave
        # a half-saved form.
        limits = {}
        for window in ("minute", "hour", "day"):
            raw = request.form.get(f"ai_limit_{window}", "").strip()
            if raw == "":
                limits[f"ai_limit_{window}"] = None  # blank = platform default
                continue
            if not raw.isdigit() or int(raw) > 100000:
                return redirect(url_for("auth.admin_user", user_id=user_id, error="limits"))
            limits[f"ai_limit_{window}"] = int(raw)

        # Suspending the owner, rejecting them or taking their admin rights
        # locks them out of their own platform just as surely as deleting the
        # account would, so the whole form is refused rather than half-applied.
        protected = not _may_touch_owner(admin, before["email"])
        if protected:
            wants_status = request.form.get("action", "") in ("reject", "suspend")
            wants_demote = request.form.get("is_admin") != "on"
            if wants_status or wants_demote:
                return redirect(url_for("auth.admin_user", user_id=user_id, error="owner"))

        action = request.form.get("action", "")
        if action in ("approve", "reject", "suspend", "reinstate"):
            new_status = {"approve": "approved", "reject": "rejected",
                          "suspend": "suspended", "reinstate": "approved"}[action]
            sb.table("user_profiles").update({
                "status": new_status, "decided_at": _now(), "decided_by": decider,
            }).eq("id", user_id).execute()
            if new_status != before["status"]:
                log_activity("admin", {"approve": "user_approved", "reject": "user_rejected",
                                       "suspend": "user_suspended", "reinstate": "user_reinstated"}[action],
                             detail={**target, "from": before["status"], "to": new_status})
            # Someone moved from pending to approved is told they can sign in.
            if new_status == "approved" and before["status"] == "pending":
                first = escape(((before.get("full_name") or "").split() or ["there"])[0])
                _send_email([before["email"]], "Your Policy Compare access is approved",
                    _email_html(
                        "You're approved",
                        f"Hi {first}, your access to Navy Health Policy Compare has been approved. You can sign in now.",
                        button="Sign in", url=f"{PUBLIC_BASE_URL}/login",
                        note_html="Sign in with your email and password, "
                                  "or ask for a one-time sign-in link on the sign-in page."))

        chosen = set(request.form.getlist("modules"))
        valid = {m["key"] for m in sb.table("modules").select("key").execute().data}
        chosen &= valid
        sb.table("user_module_access").delete().eq("user_id", user_id).execute()
        if chosen:
            sb.table("user_module_access").insert(
                [{"user_id": user_id, "module_key": k, "granted_by": decider} for k in chosen]
            ).execute()

        is_admin = request.form.get("is_admin") == "on"
        if user_id == getattr(admin, "id", None):
            is_admin = True  # an admin can't remove their own admin rights and lock themselves out
        if _is_owner(before["email"]):
            is_admin = True  # nor can the owner lose theirs, to anyone, themselves included
        sb.table("user_profiles").update({"is_admin": is_admin, **limits}).eq("id", user_id).execute()

        if chosen != before_modules:
            log_activity("admin", "modules_changed", detail={
                **target, "added": sorted(chosen - before_modules),
                "removed": sorted(before_modules - chosen), "now": sorted(chosen)})
        if is_admin != bool(before.get("is_admin")):
            log_activity("admin", "admin_granted" if is_admin else "admin_revoked", detail=target)
        old_limits = {k: before.get(k) for k in limits}
        if limits != old_limits:
            log_activity("admin", "limits_changed", detail={**target, "from": old_limits, "to": limits})

        auth.forget_user(user_id)
        return redirect(url_for("auth.admin_user", user_id=user_id, saved=1))

    rows = sb.table("user_profiles").select("*").eq("id", user_id).execute().data
    if not rows:
        abort(404)
    granted = {r["module_key"] for r in
               sb.table("user_module_access").select("module_key").eq("user_id", user_id).execute().data}
    modules = sb.table("modules").select("*").order("sort_order").execute().data
    from app.admin.routes import user_activity_summary  # late: it imports this module
    return render_template("admin/user.html", u=rows[0], modules=modules, granted=granted,
                           saved=request.args.get("saved"), error=request.args.get("error"),
                           welcome=request.args.get("welcome"), why=request.args.get("why"),
                           csrf=csrf_token(), is_self=(user_id == getattr(admin, "id", None)),
                           defaults=auth.DEFAULT_AI_LIMITS,
                           acct=request.args.get("acct"),
                           min_password=MIN_ADMIN_PASSWORD,
                           is_owner=_is_owner(rows[0].get("email")),
                           locked=not _may_touch_owner(admin, rows[0].get("email")),
                           **user_activity_summary(sb, user_id))
