"""Ending a session when an admin changes someone's password.

Supabase revokes the tokens, but this platform verifies JWTs itself rather
than asking Supabase on every request — so without a cut-off the old session
keeps working until the token expires, up to an hour later. That hour is
exactly the window that matters when the reason for the change is that an
account has been taken over.
"""
import os
import time

import pytest

os.environ.setdefault("SUPABASE_URL", "https://example.invalid")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "not-a-key")

from app import auth  # noqa: E402

NOW = time.time()


def _user(**kw):
    return auth.User(id="u1", email="a@b.c", full_name="A", phone=None,
                         status="approved", is_admin=False, **kw)


@pytest.fixture
def stub(monkeypatch):
    """resolve() without a network: one known user, one chosen token."""
    def use(user, iat):
        monkeypatch.setattr(auth, "load_user", lambda uid, **k: user)
        monkeypatch.setattr(auth, "verify_access_token",
                            lambda tok: {"sub": "u1", "iat": iat} if tok else None)
    return use


def test_a_token_from_before_the_cut_off_is_refused(stub):
    stub(_user(sessions_valid_from=NOW), NOW - 600)
    assert auth.resolve("stale-token", None).user is None


def test_a_token_from_after_the_cut_off_still_works(stub):
    stub(_user(sessions_valid_from=NOW - 600), NOW)
    assert auth.resolve("fresh-token", None).user is not None


def test_no_cut_off_means_nothing_was_ever_revoked(stub):
    stub(_user(sessions_valid_from=None), NOW - 99999)
    assert auth.resolve("old-but-valid", None).user is not None


def test_clock_skew_does_not_lock_anyone_out(stub):
    """The token is stamped by Supabase and the cut-off by us, on different
    machines. A second or two of skew must not refuse the very token someone
    was just issued when they signed in with their new password."""
    stub(_user(sessions_valid_from=NOW + 2), NOW)
    assert auth.resolve("just-issued", None).user is not None


def test_the_leeway_is_short_enough_to_be_worth_having(stub):
    """A session started shortly before the reset must still die. With the
    30s allowance this started at, resetting the password of someone who had
    just signed in did nothing at all — the session outlived the cut-off."""
    assert auth._REVOKE_LEEWAY <= 10
    stub(_user(sessions_valid_from=NOW), NOW - 15)
    assert auth.resolve("signed-in-just-before", None).user is None


def test_a_token_with_no_issued_at_is_refused_once_revoked(stub):
    """If we can't tell when it was issued, we can't tell it came after the
    cut-off — and this path only runs for an account someone has revoked."""
    stub(_user(sessions_valid_from=NOW), None)
    assert auth.resolve("no-iat", None).user is None


def test_an_unreadable_cut_off_locks_nobody_out():
    """A timestamp we can't parse must fail open. Failing closed here would
    lock out every account the moment a format changed."""
    assert auth._epoch("not a timestamp") is None
    assert auth._epoch(None) is None
    assert auth._epoch("") is None


@pytest.mark.parametrize("ts", [
    "2026-09-16T02:33:52+00:00",
    "2026-09-16T02:33:52Z",
    "2026-09-16T02:33:52.123456+00:00",
    "2026-09-16T02:33:52.123456789+00:00",   # Postgres can give nanoseconds
    "2026-09-16T02:33:52",                   # no zone: read as UTC
])
def test_the_cut_off_is_read_in_every_shape_postgres_sends(ts):
    got = auth._epoch(ts)
    assert got is not None
    assert abs(got - 1789526032) < 2          # 2026-09-16T02:33:52Z
