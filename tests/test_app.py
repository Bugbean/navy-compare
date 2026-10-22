"""The app's front door: who gets in, and where everyone else is sent.

The rule worth protecting is that it fails closed. A signed-out caller is sent
to sign in, an unapproved account to the pending page, a non-admin away from
/admin, and any path nobody mapped to a module is refused — so a page added
without a mapping is invisible rather than open to everyone.
"""
import pytest

from app import auth, create_app


def _user(**kw):
    base = dict(id="u1", email="rep@example.com", full_name="Pat Rep", phone=None,
                status="approved", is_admin=False)
    return auth.User(**{**base, **kw})


@pytest.fixture
def client(monkeypatch):
    # Nothing here may reach Supabase: activity rows are dropped, not written.
    monkeypatch.setattr(auth, "log_event", lambda **kw: None)
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture
def signed_in(monkeypatch):
    def as_(user):
        monkeypatch.setattr(auth, "resolve", lambda at, rt: auth.Resolved(user=user))
    return as_


def test_healthz_is_public_and_says_which_build(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.get_json()["build"] == "dev"


def test_signed_out_is_sent_to_sign_in(client):
    r = client.get("/")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_signed_out_api_call_gets_401_not_a_redirect(client):
    assert client.post("/api/feedback", json={}).status_code == 401


def test_the_sign_in_page_renders(client):
    r = client.get("/login")
    assert r.status_code == 200 and b"Sign in" in r.data
    assert b"Request access" not in r.data, "accounts are invite only"


def test_an_unapproved_account_waits(client, signed_in):
    signed_in(_user(status="pending"))
    r = client.get("/")
    assert r.status_code == 302 and "/auth/pending" in r.headers["Location"]


def test_an_approved_account_reaches_home(client, signed_in):
    signed_in(_user())
    r = client.get("/")
    assert r.status_code == 200 and b"Policy Compare" in r.data


def test_admin_is_refused_to_a_rep(client, signed_in):
    signed_in(_user())
    assert client.get("/admin").status_code == 403


def test_an_unmapped_path_fails_closed(client, signed_in):
    signed_in(_user(is_admin=False, modules={"call_screen"}))
    assert client.get("/something-new").status_code == 403


def test_a_module_path_needs_the_module(client, signed_in):
    signed_in(_user(modules=set()))
    assert client.get("/review").status_code == 403


def test_inviting_needs_an_admin(client, signed_in):
    signed_in(_user())
    assert client.post("/admin/users/invite", data={"email": "x@example.com"}).status_code == 403


@pytest.mark.parametrize("path", [
    "/auth/forgot",
    "/auth/callback?token_hash=abc&type=magiclink",
    "/auth/callback?token_hash=abc&type=recovery",
    "/auth/callback",
])
def test_the_signed_out_account_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200 and b"Policy Compare" in r.data


def test_the_pending_page_renders(client, signed_in):
    signed_in(_user(status="pending"))
    assert client.get("/auth/pending").status_code == 200


def test_no_self_registration_route_exists(client):
    assert client.get("/register").status_code in (302, 404)
