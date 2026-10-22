"""Why people get signed out unexpectedly: the refresh token.

Supabase rotates it on every use: the token presented is spent, and a new
one comes back. The call screen fires several htmx requests at once, each
carrying the same cookie.

So when the access token expired, every request in flight called Supabase
with the same refresh token at once. One won and rotated it; the rest
presented a spent token, were refused, and "refused" reads to the caller as
"not signed in". More tabs make it more likely, which is why it looks random
and finds the heaviest users first.

These cover the three faults together: refreshing once per token rather than
once per request, renewing early while the token still works instead of at
the moment it dies, and never dropping a rotation that has already happened.
"""
import os
import threading
import time

import pytest

os.environ.setdefault("SUPABASE_URL", "https://example.invalid")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "not-a-key")

from app import auth  # noqa: E402


def session(tag="new"):
    return auth.Session(access_token=f"at-{tag}", refresh_token=f"rt-{tag}",
                            expires_in=3600, user_id="u1")


def approved():
    return auth.User(id="u1", email="a@b.c", full_name="A", phone=None,
                         status="approved", is_admin=False)


@pytest.fixture(autouse=True)
def clean_slate(monkeypatch):
    """The share is process-wide, so each test starts with it empty."""
    auth._refresh_done.clear()
    auth._refresh_inflight.clear()
    monkeypatch.setattr(auth, "load_user", lambda uid, **kw: approved())
    yield
    auth._refresh_done.clear()
    auth._refresh_inflight.clear()


# ── one refresh, however many ask ─────────────────────────────────────────

def test_a_burst_of_requests_spends_the_token_once(monkeypatch):
    """The fault in one test: twenty requests, one rotation."""
    calls = []
    started = threading.Barrier(20)

    def slow_refresh(rt):
        calls.append(rt)
        time.sleep(0.05)
        return session()

    monkeypatch.setattr(auth, "refresh", slow_refresh)

    out = []
    def ask():
        started.wait()
        out.append(auth.refresh_shared("rt-old"))

    threads = [threading.Thread(target=ask) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1, f"the token was spent {len(calls)} times"
    assert len(out) == 20
    assert all(s is not None for s in out), "a request was refused its session"
    assert {s.access_token for s in out} == {"at-new"}


def test_a_straggler_still_holding_the_spent_token_is_not_refused(monkeypatch):
    """The normal case when several requests were in flight together: one
    arrives late, still carrying the token that has already been rotated."""
    monkeypatch.setattr(auth, "refresh", lambda rt: session())
    first = auth.refresh_shared("rt-old")
    monkeypatch.setattr(auth, "refresh", lambda rt: pytest.fail(
        "the spent token was sent to Supabase a second time"))
    late = auth.refresh_shared("rt-old")
    assert late is first


def test_a_failed_refresh_is_not_retried_for_every_request(monkeypatch):
    calls = []
    monkeypatch.setattr(auth, "refresh",
                        lambda rt: calls.append(rt) or None)
    assert auth.refresh_shared("rt-dead") is None
    assert auth.refresh_shared("rt-dead") is None
    assert len(calls) == 1


def test_nothing_to_refresh_asks_nobody(monkeypatch):
    monkeypatch.setattr(auth, "refresh", lambda rt: pytest.fail("called"))
    assert auth.refresh_shared(None) is None
    assert auth.refresh_shared("") is None


# ── renewed early, not at the graveside ───────────────────────────────────

def test_a_token_with_life_left_is_left_alone(monkeypatch):
    monkeypatch.setattr(auth, "verify_access_token",
                        lambda t: {"sub": "u1", "exp": time.time() + 3000})
    monkeypatch.setattr(auth, "refresh", lambda rt: pytest.fail(
        "refreshed a token that had fifty minutes left"))
    got = auth.resolve("at-live", "rt-live")
    assert got.user is not None
    assert got.new_session is None


def test_a_token_about_to_expire_is_renewed_before_it_does(monkeypatch):
    """Renewing at the moment of expiry puts the rotation exactly where a
    returning browser fires a burst of requests."""
    def verify(token):
        if token == "at-old":
            return {"sub": "u1", "exp": time.time() + 60}
        return {"sub": "u1", "exp": time.time() + 3600}

    monkeypatch.setattr(auth, "verify_access_token", verify)
    monkeypatch.setattr(auth, "refresh", lambda rt: session())
    got = auth.resolve("at-old", "rt-old")
    assert got.user is not None
    assert got.new_session is not None, "it waited for the token to die"
    assert got.new_session.access_token == "at-new"


def test_an_early_renewal_that_fails_changes_nothing(monkeypatch):
    """The token in hand is still valid, so a failed renewal must not sign
    anybody out. The next request tries again."""
    monkeypatch.setattr(auth, "verify_access_token",
                        lambda t: {"sub": "u1", "exp": time.time() + 60})
    monkeypatch.setattr(auth, "refresh", lambda rt: None)
    got = auth.resolve("at-old", "rt-old")
    assert got.user is not None, "a failed early renewal signed the user out"
    assert got.new_session is None


@pytest.mark.parametrize("left,renew", [(30, True), (299, True), (301, False), (3599, False)])
def test_where_the_line_sits(monkeypatch, left, renew):
    monkeypatch.setattr(auth, "verify_access_token",
                        lambda t: {"sub": "u1", "exp": time.time() + left})
    monkeypatch.setattr(auth, "refresh", lambda rt: session())
    got = auth.resolve("at-old", "rt-old")
    assert (got.new_session is not None) is renew


# ── a rotation that has happened is never dropped ─────────────────────────

def test_a_rotation_is_handed_back_even_when_the_request_cannot_be_authorised(monkeypatch):
    """Supabase has already spent the old token. Returning without the new
    one leaves the browser holding a dead token - signed out for good."""
    monkeypatch.setattr(auth, "verify_access_token", lambda t: None)
    monkeypatch.setattr(auth, "refresh", lambda rt: session())
    got = auth.resolve("at-expired", "rt-old")
    assert got.user is None
    assert got.new_session is not None, "the rotated session was thrown away"
    assert got.new_session.refresh_token == "rt-new"


def test_no_refresh_token_at_all_is_simply_not_signed_in(monkeypatch):
    monkeypatch.setattr(auth, "verify_access_token", lambda t: None)
    got = auth.resolve("at-expired", None)
    assert got.user is None and got.new_session is None


# ── the guarantees that must survive all of this ──────────────────────────

def test_an_expired_session_still_resolves_through_a_refresh(monkeypatch):
    def verify(token):
        return None if token == "at-expired" else {"sub": "u1", "exp": time.time() + 3600}

    monkeypatch.setattr(auth, "verify_access_token", verify)
    monkeypatch.setattr(auth, "refresh", lambda rt: session())
    got = auth.resolve("at-expired", "rt-old")
    assert got.user is not None
    assert got.new_session.access_token == "at-new"


def test_an_admin_cut_off_still_ends_the_session(monkeypatch):
    """Renewing early must not become a way around a revocation."""
    issued = time.time() - 10
    monkeypatch.setattr(auth, "verify_access_token",
                        lambda t: {"sub": "u1", "exp": time.time() + 60, "iat": issued})
    monkeypatch.setattr(auth, "refresh", lambda rt: session())
    monkeypatch.setattr(auth, "load_user",
                        lambda uid, **kw: auth.User(
                            id="u1", email="a@b.c", full_name="A", phone=None,
                            status="approved", is_admin=False,
                            sessions_valid_from=time.time()))
    assert auth.resolve("at-old", "rt-old").user is None
