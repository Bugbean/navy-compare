"""
Authentication and authorisation for Navy Health Policy Compare.

Framework-neutral on purpose: everything here takes and returns plain values,
and app/auth/routes.py does the Flask request/response cookie handling.

Built on Supabase Auth (GoTrue), which owns passwords, magic links, reset
emails and token issuance. This module adds:

  * local verification of access tokens against Supabase's public JWKS
    (ES256), so no secret is shared and no network call happens per request
  * the approval gate and per-module permissions (migration 0001)
  * thin wrappers over the GoTrue endpoints the login flows need

Sessions live in two host-only cookies unless SESSION_COOKIE_DOMAIN is set.
"""

from __future__ import annotations

import concurrent.futures as _futures
import json
import logging as _logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

import jwt
from jwt import PyJWKClient

# ── Configuration ─────────────────────────────────────────────────────────────

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

COOKIE_ACCESS = "nc_at"
COOKIE_REFRESH = "nc_rt"

# Unset locally → host-only cookie, which is what you want on localhost.
COOKIE_DOMAIN = os.environ.get("SESSION_COOKIE_DOMAIN") or None
COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "true").lower() != "false"

# How long a loaded profile is trusted before re-reading it. This is the
# upper bound on how long a suspension takes to bite on *other* instances;
# the instance handling the admin action invalidates immediately.
USER_CACHE_SECONDS = 30

# Tokens are stamped by Supabase and the cut-off by us, on different machines,
# so a little clock skew has to be tolerated — typically about a second, which
# verify_access_token's 30s allowance comfortably covers.
#
# But the two allowances guard opposite risks and must not be the same number.
# There, too little leeway rejects a brand-new token and nobody can sign in at
# all. Here, too much leaves a session alive that an admin has just revoked:
# any token issued within the allowance of the cut-off survives it. At 30s
# that is most of them — resetting the password of someone who had just
# signed in would do nothing. Five seconds is several times the usual skew
# and closes the window to something too short to matter.
_REVOKE_LEEWAY = 5

STATUSES = ("pending", "approved", "rejected", "suspended")


def cookie_kwargs(max_age: int | None = None) -> dict:
    """Cookie attributes for the session cookies."""
    kw = {"httponly": True, "secure": COOKIE_SECURE, "samesite": "Lax", "path": "/"}
    if COOKIE_DOMAIN:
        kw["domain"] = COOKIE_DOMAIN
    if max_age is not None:
        kw["max_age"] = max_age
    return kw


# ── Token verification ────────────────────────────────────────────────────────

_jwks_client: PyJWKClient | None = None


def _jwks() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(
            f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json",
            cache_keys=True,
            headers={"apikey": _SERVICE_KEY},
        )
    return _jwks_client


def verify_access_token(token: str | None) -> dict | None:
    """Claims for a valid, unexpired Supabase access token, else None.

    Verified locally against the published ES256 public key. An expired
    token returns None — callers then try the refresh token.

    Thirty seconds of clock difference is allowed. Supabase stamps a token
    the instant it signs someone in, so a machine whose clock is even a
    second behind Supabase's rejects that brand-new token as "not yet
    valid" and sends the person straight back to the sign-in page. Thirty
    seconds is the usual allowance and is nothing against a token's hour.
    """
    if not token:
        return None
    try:
        key = _jwks().get_signing_key_from_jwt(token)
        return jwt.decode(
            token, key.key,
            algorithms=["ES256", "RS256"],
            audience="authenticated",
            leeway=30,
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError:
        return None


# ── GoTrue (Supabase Auth) calls ──────────────────────────────────────────────

class AuthError(Exception):
    """A GoTrue call failed. `message` is safe to show the user."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _gotrue(method: str, path: str, body: dict | None = None,
            bearer: str | None = None) -> dict:
    req = urllib.request.Request(
        f"{SUPABASE_URL}/auth/v1{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "apikey": _SERVICE_KEY,
            "Authorization": f"Bearer {bearer or _SERVICE_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}")
        except ValueError:
            detail = {}
        msg = (detail.get("msg") or detail.get("error_description")
               or detail.get("message") or "Authentication failed")
        raise AuthError(msg, e.code) from None


@dataclass
class Session:
    access_token: str
    refresh_token: str
    expires_in: int
    user_id: str


def _session_from(resp: dict) -> Session:
    if not resp.get("access_token"):
        raise AuthError("No session returned", 400)
    return Session(
        access_token=resp["access_token"],
        refresh_token=resp["refresh_token"],
        expires_in=int(resp.get("expires_in", 3600)),
        user_id=(resp.get("user") or {}).get("id", ""),
    )


def sign_in_password(email: str, password: str) -> Session:
    return _session_from(_gotrue("POST", "/token?grant_type=password",
                                 {"email": email, "password": password}))


def send_magic_link(email: str, redirect_to: str) -> None:
    """Login-only: create_user is False, so a magic link to an address with no
    account creates nothing. Accounts are made by invitation only."""
    try:
        _gotrue("POST", f"/otp?redirect_to={urllib.parse.quote(redirect_to, safe='')}",
                {"email": email, "create_user": False})
    except AuthError:
        # Deliberately swallowed — reporting "no such account" would let
        # anyone probe which addresses are registered.
        pass


def send_password_reset(email: str, redirect_to: str) -> None:
    try:
        _gotrue("POST", f"/recover?redirect_to={urllib.parse.quote(redirect_to, safe='')}",
                {"email": email})
    except AuthError:
        pass  # same enumeration concern as send_magic_link


def recovery_token(email: str) -> str:
    """A one-time password-setting token for an existing account, made with
    the admin API so NOTHING is sent: the caller puts it in its own email
    (the welcome email) as /auth/callback?token_hash=…&type=recovery. It
    replaces any earlier recovery token for that account, and expires on the
    project's email-link expiry."""
    resp = _gotrue("POST", "/admin/generate_link", {"type": "recovery", "email": email})
    token = resp.get("hashed_token") or (resp.get("properties") or {}).get("hashed_token")
    if not token:
        raise AuthError("No token in the generate_link response", 500)
    return token


def verify_email_link(token_hash: str, link_type: str) -> Session:
    """Exchanges the token_hash from a confirmation / magic-link / recovery
    email for a session. link_type is 'signup', 'magiclink', 'recovery' or
    'email'."""
    return _session_from(_gotrue("POST", "/verify",
                                 {"token_hash": token_hash, "type": link_type}))


def refresh(refresh_token: str | None) -> Session | None:
    if not refresh_token:
        return None
    try:
        return _session_from(_gotrue("POST", "/token?grant_type=refresh_token",
                                     {"refresh_token": refresh_token}))
    except AuthError:
        return None



# ── Refreshing, once ──────────────────────────────────────────────────────────
#
# Supabase rotates the refresh token on use: the one presented is spent and a
# new one comes back. The call screen fires several htmx requests at once, so
# without coordination a burst of requests all spend the same token together,
# one wins, and the rest are told their token is invalid - which reads to the
# caller as "not signed in".

# Renew while the token in hand still works, rather than after it has died.
# Doing it at the moment of expiry puts the rotation exactly where a
# returning browser fires a burst of requests.
REFRESH_WHEN_UNDER = 300

# How long one rotation's result answers for the token that was spent. Long
# enough for a straggler from the same burst, short enough that a genuinely
# stale cookie is not honoured.
_REFRESH_GRACE = 90

_refresh_lock = threading.Lock()
_refresh_inflight: dict[str, threading.Event] = {}
_refresh_done: dict[str, tuple[float, "Session | None"]] = {}


def _expiring_soon(claims: dict) -> bool:
    exp = claims.get("exp")
    return bool(exp) and (float(exp) - time.time()) < REFRESH_WHEN_UNDER


def refresh_shared(refresh_token: str | None) -> "Session | None":
    """Refresh once per token, however many requests ask at the same time.

    The first caller does the work; the rest wait for it and take the same
    answer. The answer is then held for a short grace period, so a request
    that arrives still holding the spent token - which is the normal case
    when several were in flight together - is given the session that token
    turned into rather than being told it is invalid.
    """
    if not refresh_token:
        return None

    now = time.monotonic()
    with _refresh_lock:
        hit = _refresh_done.get(refresh_token)
        if hit and now - hit[0] < _REFRESH_GRACE:
            return hit[1]
        waiting = _refresh_inflight.get(refresh_token)
        if waiting is None:
            waiting = threading.Event()
            _refresh_inflight[refresh_token] = waiting
            ours = True
        else:
            ours = False

    if not ours:
        # Someone else is already spending this token. Their answer is ours.
        waiting.wait(timeout=20)
        with _refresh_lock:
            hit = _refresh_done.get(refresh_token)
        return hit[1] if hit else None

    session = None
    try:
        session = refresh(refresh_token)
    finally:
        with _refresh_lock:
            _refresh_done[refresh_token] = (time.monotonic(), session)
            _refresh_inflight.pop(refresh_token, None)
            if len(_refresh_done) > 500:
                cutoff = time.monotonic() - _REFRESH_GRACE
                for spent in [k for k, v in _refresh_done.items() if v[0] < cutoff]:
                    _refresh_done.pop(spent, None)
        waiting.set()
    return session


def admin_get_user(user_id: str) -> dict:
    """The Supabase Auth record for an account (service role)."""
    return _gotrue("GET", f"/admin/users/{urllib.parse.quote(user_id)}")


def admin_update_user(user_id: str, changes: dict) -> dict:
    """Change an account as the service role: {"password": …} and/or
    {"user_metadata": {…}} (keys merge; a None value removes that key)."""
    return _gotrue("PUT", f"/admin/users/{urllib.parse.quote(user_id)}", changes)


def admin_list_users() -> list[dict]:
    """Every Supabase Auth account (service role). One page of 1000 — far
    more accounts than the platform has."""
    return _gotrue("GET", "/admin/users?page=1&per_page=1000").get("users") or []


def update_password(access_token: str, new_password: str) -> None:
    _gotrue("PUT", "/user", {"password": new_password}, bearer=access_token)


def sign_out(access_token: str | None) -> None:
    """Revokes the refresh token server-side, so a stolen cookie can't be
    refreshed after sign-out."""
    if not access_token:
        return
    try:
        _gotrue("POST", "/logout", {}, bearer=access_token)
    except AuthError:
        pass


# ── Profile and permissions ───────────────────────────────────────────────────

@dataclass
class User:
    id: str
    email: str
    full_name: str | None
    phone: str | None
    status: str
    is_admin: bool
    modules: set[str] = field(default_factory=set)
    # Tokens issued before this are refused — see resolve(). Set when an admin
    # changes someone's password or sign-in address.
    sessions_valid_from: float | None = None

    @property
    def approved(self) -> bool:
        return self.status == "approved"

    def can(self, module: str) -> bool:
        return self.is_admin or module in self.modules

    @property
    def display_name(self) -> str:
        return self.full_name or self.email


_user_cache: dict[str, tuple[float, User]] = {}
_cache_lock = threading.Lock()


def _rest(path: str) -> list:
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1{path}",
        headers={"apikey": _SERVICE_KEY, "Authorization": f"Bearer {_SERVICE_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read() or b"[]")


def _epoch(ts) -> float | None:
    """A Postgres timestamptz as seconds since the epoch. None if absent or
    unparseable — a cut-off we can't read must not lock anyone out."""
    if not ts:
        return None
    try:
        s = str(ts).replace("Z", "+00:00")
        if "." in s:                       # Postgres gives microseconds; trim to 6
            head, _, tail = s.partition(".")
            digits = "".join(c for c in tail if c.isdigit())[:6]
            rest = tail[len(digits):] if tail[len(digits):].startswith(("+", "-")) else ""
            s = f"{head}.{digits}{rest}" if digits else head + rest
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def load_user(user_id: str, *, use_cache: bool = True) -> User | None:
    """Profile + granted modules for a user id, or None if no profile."""
    now = time.monotonic()
    if use_cache:
        with _cache_lock:
            hit = _user_cache.get(user_id)
            if hit and now - hit[0] < USER_CACHE_SECONDS:
                return hit[1]

    uid = urllib.parse.quote(user_id, safe="")
    rows = _rest(f"/user_profiles?id=eq.{uid}&select=*")
    if not rows:
        return None
    p = rows[0]
    mods = _rest(f"/user_module_access?user_id=eq.{uid}&select=module_key")
    user = User(
        id=p["id"], email=p["email"], full_name=p.get("full_name"),
        phone=p.get("phone"), status=p["status"], is_admin=bool(p.get("is_admin")),
        modules={m["module_key"] for m in mods},
        sessions_valid_from=_epoch(p.get("sessions_valid_from")),
    )
    with _cache_lock:
        _user_cache[user_id] = (now, user)
    return user


def forget_user(user_id: str) -> None:
    """Drop a cached profile — call after changing someone's status or modules
    so the change applies immediately on this instance."""
    with _cache_lock:
        _user_cache.pop(user_id, None)


# ── Resolving a request ───────────────────────────────────────────────────────

@dataclass
class Resolved:
    """What a request's cookies amount to.

    `new_session` is set when the access token had expired and was refreshed
    — the caller must write the new cookies onto the response, or the user
    will be refreshed again on every request until the refresh token expires.
    """
    user: User | None
    new_session: Session | None = None


def resolve(access_token: str | None, refresh_token: str | None) -> Resolved:
    claims = verify_access_token(access_token)
    new_session = None
    if claims is None:
        new_session = refresh_shared(refresh_token)
        if new_session is None:
            return Resolved(user=None)
        claims = verify_access_token(new_session.access_token)
        if claims is None:
            # The rotation happened whatever we make of the result: the token
            # in the browser is spent. Hand the new one back even though this
            # request cannot be authorised, or the session is lost for good.
            return Resolved(user=None, new_session=new_session)
    elif _expiring_soon(claims):
        # Renewed early, while the token in hand still works. If this fails
        # nothing breaks - the existing token is still valid, and the next
        # request tries again.
        early = refresh_shared(refresh_token)
        if early is not None:
            fresh = verify_access_token(early.access_token)
            if fresh is not None:
                new_session, claims = early, fresh

    user = load_user(claims["sub"])
    # A token stays cryptographically valid until it expires, about an hour,
    # whatever the auth server thinks of it — and this is verified here, not
    # asked of Supabase, so a revoked session would otherwise keep working for
    # the rest of that hour. If an admin has set a cut-off for this account
    # anything issued before it is refused.
    if user is not None and user.sessions_valid_from:
        issued = claims.get("iat")
        if issued is None or float(issued) < user.sessions_valid_from - _REVOKE_LEEWAY:
            # A fresh token minted from a revoked refresh token would be worse
            # than useless: hand back nothing at all.
            return Resolved(user=None)
    return Resolved(user=user, new_session=new_session)


# ── Activity log ──────────────────────────────────────────────

_log = _logging.getLogger("app.auth")

# Page views go through this so browsing doesn't wait on a database write.
# Small and bounded: under a burst, excess page views queue rather than
# spawning unbounded threads.
_bg = _futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="activity-log")


def _rest_read(path: str):
    """A GET against PostgREST as the service role."""
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1{path}",
        headers={"apikey": _SERVICE_KEY, "Authorization": f"Bearer {_SERVICE_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read()
        return json.loads(raw) if raw else []


def _rest_write(method: str, path: str, body, prefer: str = "return=minimal"):
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1{path}",
        data=json.dumps(body).encode(),
        method=method,
        headers={
            "apikey": _SERVICE_KEY,
            "Authorization": f"Bearer {_SERVICE_KEY}",
            "Content-Type": "application/json",
            "Prefer": prefer,
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def client_ip(forwarded_for: str | None, fallback: str | None = None) -> str | None:
    """The caller's address as the load balancer saw it.

    Google's external load balancer sends X-Forwarded-For as
    "<whatever the client sent>, <client ip>, <load balancer ip>". The
    leftmost entries are the caller's own claim and can be forged, so the
    trustworthy one is second from the right. A request that reached Cloud
    Run directly (its run.app address) has just the one entry Cloud Run
    added.
    """
    parts = [p.strip() for p in (forwarded_for or "").split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0] if parts else fallback


def log_event(*, service: str, category: str, event: str,
              user: "User | None" = None, email: str | None = None,
              path: str | None = None, method: str | None = None,
              status: int | None = None, ip: str | None = None,
              user_agent: str | None = None, detail: dict | None = None,
              background: bool = False, **ai) -> int | None:
    """Record one row in activity_log and return its id.

    Security-relevant events (auth, admin, access, ai) are written
    synchronously so they can't be lost. Pass background=True only for page
    views, where losing the odd row on an instance shutdown is acceptable.

    Never raises: a logging failure must not break the request it describes.
    """
    if path and path.startswith("/auth/welcome/") and "." in path:
        # A welcome link's path carries the secret that lets whoever holds it
        # set that account's password: keep only the account id.
        path = path.split(".", 1)[0] + ".…"
    row = {
        "service": service, "category": category, "event": event,
        "user_id": user.id if user else None,
        "user_email": (user.email if user else None) or email,
        "path": (path or "")[:500] or None, "method": method, "status": status,
        "ip": ip, "user_agent": (user_agent or "")[:300] or None,
        "detail": detail,
        **{k: v for k, v in ai.items() if k in (
            "ai_model", "ai_tokens_in", "ai_tokens_out", "ai_cost_usd", "latency_ms")},
    }

    def _write():
        try:
            res = _rest_write("POST", "/activity_log", row, prefer="return=representation")
            return res[0]["id"] if res else None
        except Exception as e:
            _log.warning("activity_log write failed (%s/%s): %s", category, event, e)
            return None

    if background:
        _bg.submit(_write)
        return None
    return _write()


def should_alert(kind: str, quiet_minutes: int = 30, *, service: str = "web", detail: dict | None = None) -> bool:
    """True the first time something worth an email goes wrong, then False
    for the quiet period.

    The window is counted from activity_log (each alert is written there as
    ai_alert_sent), so every instance and job shares it: a
    per-process counter would send one email per Cloud Run instance, and a
    broken model would fill an inbox. Never raises — if the check itself
    fails, the alert goes out.
    """
    from datetime import datetime, timedelta, timezone
    try:
        since = (datetime.now(timezone.utc) - timedelta(minutes=quiet_minutes)).isoformat()
        rows = _rest_read(f"/activity_log?event=eq.ai_alert_sent&occurred_at=gte.{since}&select=id&limit=1")
        if rows:
            return False
    except Exception as e:
        _log.warning("could not check the alert quiet period: %s", e)
    log_event(service=service, category="ai", event="ai_alert_sent",
              detail={"kind": kind, **(detail or {})})
    return True


def run_in_background(fn, *args, **kwargs) -> None:
    """Run something that must neither delay nor break the request it came
    from — an alert email, say. Failures are logged, never raised."""
    def _run():
        try:
            fn(*args, **kwargs)
        except Exception as e:
            _log.error("background task %s failed: %s", getattr(fn, "__name__", fn), e)
    try:
        _bg.submit(_run)
    except Exception as e:
        _log.error("could not start background task: %s", e)


def update_event(log_id: int | None, *, background: bool = False, **fields) -> None:
    """Fill in a row after the fact — used to complete an AI question once
    its answer has finished streaming (tokens, cost, latency, final event).

    background=True is for cleanup paths that can't await, such as a stream
    torn down because the browser disconnected."""
    if not log_id:
        return

    def _write():
        try:
            _rest_write("PATCH", f"/activity_log?id=eq.{int(log_id)}", fields)
        except Exception as e:
            _log.warning("activity_log update %s failed: %s", log_id, e)

    if background:
        _bg.submit(_write)
    else:
        _write()


# ── AI rate limiting ──────────────────────────────────────────────────────────

DEFAULT_AI_LIMITS = {"minute": 10, "hour": 60, "day": 300}


@dataclass
class LimitResult:
    allowed: bool
    window: str | None = None       # which limit tripped: minute / hour / day
    limit: int | None = None
    used: int | None = None
    retry_after: int | None = None  # seconds

    @property
    def message(self) -> str:
        if self.allowed:
            return ""
        # Every window is rolling, so "day" is the last 24 hours, not since
        # midnight; the message mustn't promise a reset at midnight.
        return {
            "minute": f"You've reached your limit of {self.limit} AI questions a minute. "
                      "Try again in a minute.",
            "hour":   f"You've reached your limit of {self.limit} AI questions an hour. "
                      "Try again a little later.",
            "day":    f"You've reached your limit of {self.limit} AI questions in 24 hours. "
                      "Try again later, or ask the platform administrator to raise your limit.",
        }[self.window]


def _user_limits(user: "User") -> dict:
    try:
        rows = _rest(f"/user_profiles?id=eq.{urllib.parse.quote(user.id, safe='')}"
                     "&select=ai_limit_minute,ai_limit_hour,ai_limit_day")
        r = rows[0] if rows else {}
    except Exception:
        r = {}
    return {
        "minute": r.get("ai_limit_minute") if r.get("ai_limit_minute") is not None else DEFAULT_AI_LIMITS["minute"],
        "hour":   r.get("ai_limit_hour")   if r.get("ai_limit_hour")   is not None else DEFAULT_AI_LIMITS["hour"],
        "day":    r.get("ai_limit_day")    if r.get("ai_limit_day")    is not None else DEFAULT_AI_LIMITS["day"],
    }


def check_ai_limit(user: "User | None") -> LimitResult:
    """Whether this person may ask another AI question right now.

    Counts come from activity_log in one RPC call. If the count can't be
    read, this FAILS OPEN — refusing everyone because the database hiccuped
    would take the call screen down mid-call, and the per-question log still
    records the usage.
    """
    if user is None:
        return LimitResult(allowed=False, window="minute", limit=0, used=0, retry_after=60)
    try:
        res = _rest_write("POST", "/rpc/fn_ai_usage_counts", {"p_user_id": user.id},
                          prefer="return=representation")
        counts = res[0] if isinstance(res, list) and res else (res or {})
    except Exception as e:
        _log.warning("rate-limit count failed for %s, failing open: %s", user.id, e)
        return LimitResult(allowed=True)

    limits = _user_limits(user)
    for window, key, wait in (("minute", "per_minute", 60),
                              ("hour", "per_hour", 3600),
                              ("day", "per_day", 86400)):
        used = int(counts.get(key) or 0)
        if used >= limits[window]:
            return LimitResult(allowed=False, window=window, limit=limits[window],
                               used=used, retry_after=wait)
    return LimitResult(allowed=True)


# ── AI cost ───────────────────────────────────────────────────────────────────

# USD per million tokens, Anthropic first-party API. Taken from the Claude API
# reference (models table, cached 2026-10-06) on 23 Oct 2026 — not recalled.
# Haiku 5.5's rate is for prompts of 100K tokens or fewer, which every call
# here is. Matched by prefix so dated IDs price the same as their family. An
# unlisted model records tokens but no cost, rather than a guessed one.
#
# If CLAUDE_BACKEND=vertex is ever switched on, Vertex bills separately (and
# regional endpoints carry a premium), so these become estimates.
AI_PRICES = {
    "claude-haiku-5-5":  (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-5":   (2.00, 10.00),
    "claude-haiku-4-5":  (1.00, 5.00),
    # Longest prefix first: "claude-opus-5" would otherwise price Opus 5.5 too.
    "claude-opus-5-5":   (4.00, 20.00),
    "claude-opus-5":     (5.00, 25.00),
}
_CACHE_READ = 0.10    # × input price
_CACHE_WRITE = 1.25   # × input price, 5-minute TTL


def _price(model: str | None):
    for prefix, p in AI_PRICES.items():
        if model and model.startswith(prefix):
            return p
    return None


def ai_usage(model: str | None, input_tokens: int = 0, output_tokens: int = 0,
             cache_write: int = 0, cache_read: int = 0) -> tuple[dict, dict]:
    """From one call's (or a summed) token usage, return:

      columns — ai_model / ai_tokens_in / ai_tokens_out / ai_cost_usd, ready to
                pass straight to log_event() or update_event()
      split   — the raw breakdown, to store under detail["usage"] so cost can
                be recomputed later if prices change

    input_tokens from the API is the UNCACHED remainder only — the real prompt
    size is input + cache_write + cache_read, each priced differently.
    """
    i, o = input_tokens or 0, output_tokens or 0
    w, r = cache_write or 0, cache_read or 0
    cost = None
    p = _price(model)
    if p:
        pin, pout = p
        cost = round((i * pin + w * pin * _CACHE_WRITE + r * pin * _CACHE_READ + o * pout)
                     / 1_000_000, 6)
    columns = {"ai_model": model, "ai_tokens_in": i + w + r,
               "ai_tokens_out": o, "ai_cost_usd": cost}
    split = {"input": i, "cache_write": w, "cache_read": r, "output": o}
    return columns, split


def sum_usage(usages) -> dict:
    """Add up the usage objects from several API calls (an agentic loop's
    rounds). Accepts SDK Usage objects or dicts with the same field names."""
    total = {"input_tokens": 0, "output_tokens": 0,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    for u in usages:
        if u is None:
            continue
        for k in total:
            v = u.get(k) if isinstance(u, dict) else getattr(u, k, None)
            total[k] += v or 0
    return total


def module_for_path(path: str, route_modules: list[tuple[str, str]]) -> str | None:
    """First matching (prefix, module) wins, so list specific prefixes before
    general ones. Returns None for an unmapped path — callers must treat that
    as denied, so a new route added without a mapping fails closed."""
    for prefix, module in route_modules:
        base = prefix.rstrip("/")
        # Exact, or a true sub-path — "/call" must match "/call/123" but not
        # "/calls", or the wrong module's permission would apply.
        if path == base or path == prefix or path.startswith(base + "/"):
            return module
    return None
