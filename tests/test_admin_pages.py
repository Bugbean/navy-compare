"""Every admin page renders.

The admin templates link to each other with url_for. A link to a page that does
not exist only fails when the page is opened, so each one is opened here against
a fake Supabase.
"""
import pytest

from app import auth, create_app

ADMIN = auth.User(id="00000000-0000-0000-0000-000000000001", email="admin@example.com",
                  full_name="Ada Admin", phone=None, status="approved", is_admin=True)

PROFILE = {"id": "00000000-0000-0000-0000-000000000002", "email": "rep@example.com",
           "full_name": "Pat Rep", "phone": None, "status": "approved", "is_admin": False,
           "created_at": "2026-10-23T01:00:00+00:00", "confirmed_at": "2026-10-23T01:00:00+00:00",
           "decided_at": None, "ai_limit_minute": None, "ai_limit_hour": None, "ai_limit_day": None}


class Result:
    def __init__(self, data):
        self.data, self.count = data, len(data) if isinstance(data, list) else 0


class Query:
    """Answers any PostgREST chain with rows shaped like the table's."""
    ROWS = {
        "user_profiles": [PROFILE],
        "modules": [{"key": "call_screen", "label": "Call screen", "layer": "rep", "sort_order": 10},
                    {"key": "review_queue", "label": "Identity review queue", "layer": "lead", "sort_order": 110}],
        "user_module_access": [{"module_key": "call_screen"}],
        "activity_log": [{"id": 1, "occurred_at": "2026-10-23T01:00:00+00:00", "service": "web",
                          "user_id": PROFILE["id"], "user_email": PROFILE["email"], "category": "auth",
                          "event": "sign_in", "path": "/login", "detail": {"method": "password"},
                          "ai_cost_usd": None}],
        "feedback": [{"id": 1, "created_at": "2026-10-23T01:00:00+00:00", "kind": "bug",
                      "message": "The card is wrong.", "site": "web", "page_key": "call_screen",
                      "page_url": "/card", "user_agent": "x", "user_id": PROFILE["id"],
                      "user_email": PROFILE["email"], "user_name": "Pat Rep", "emailed": True,
                      "status": "new", "notes": None}],
        "job_runs": [{"job_name": "migrate", "status": "success", "started_at": "2026-10-23T01:00:00+00:00"}],
        "v_user_last_activity": [],
    }

    def __init__(self, name):
        self.name = name

    def __getattr__(self, _):
        return lambda *a, **k: self

    @property
    def not_(self):
        return self

    def execute(self):
        return Result([dict(r) for r in self.ROWS.get(self.name, [])])


class FakeSB:
    def table(self, name):
        return Query(name)

    def rpc(self, name, params):
        data = {} if name == "fn_admin_overview" else []
        return type("R", (), {"execute": lambda self: Result(data)})()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "log_event", lambda **kw: None)
    monkeypatch.setattr(auth, "resolve", lambda at, rt: auth.Resolved(user=ADMIN))
    import app.db
    monkeypatch.setattr(app.db, "get_sb", lambda: FakeSB())
    a = create_app()
    a.config.update(TESTING=True)
    return a.test_client()


@pytest.mark.parametrize("path", [
    "/admin",
    "/admin/activity",
    "/admin/activity?category=all",
    "/admin/usage",
    "/admin/feedback",
    "/admin/users",
    "/admin/users?invite=exists",
    f"/admin/users/{PROFILE['id']}",
    f"/admin/users/{PROFILE['id']}?welcome=sent&acct=saved",
    "/",
])
def test_admin_page_renders(client, path):
    r = client.get(path)
    assert r.status_code == 200, r.data[:500]
