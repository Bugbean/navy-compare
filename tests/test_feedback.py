"""Feedback from the people using the app: what gets stored, and what gets sent.

The property worth protecting is that a report is never lost. The email is a
notification and notifications fail — a key expires, Resend has an outage, the
service was deployed without the secret. None of that may cost the report, and
none of it may tell the person their report went through when it did not.
"""
import pytest

from app.auth import feedback


class FakeUser:
    id = "11111111-2222-3333-4444-555555555555"
    email = "pat@example.com"
    display_name = "Pat Rep"


class FakeTable:
    def __init__(self, store, name):
        self.store, self.name, self._update, self._eq = store, name, None, None

    def insert(self, row):
        self.store.setdefault(self.name, []).append(dict(row))
        self._result = [{**row, "id": len(self.store[self.name])}]
        return self

    def update(self, patch):
        self._update = patch
        self._result = []
        return self

    def eq(self, col, val):
        self._eq = (col, val)
        return self

    def execute(self):
        if self._update is not None:
            for r in self.store.get(self.name, []):
                r.update(self._update)
        return type("R", (), {"data": self._result})()


class FakeSB:
    """Just enough of the client to record what would have been written."""

    def __init__(self, fail=False):
        self.store, self.fail = {}, fail

    def table(self, name):
        if self.fail:
            raise RuntimeError("database unreachable")
        return FakeTable(self.store, name)


@pytest.fixture
def sent(monkeypatch):
    """Capture outbound mail instead of sending it."""
    box = []

    def fake_send(to, subject, html, reply_to=None, sender=None):
        box.append({"to": to, "subject": subject, "html": html, "reply_to": reply_to})
        return True

    monkeypatch.setattr(feedback.mailer, "send", fake_send)
    monkeypatch.setattr(feedback, "TO", ["team@example.com"])
    return box


# ── What may be filed ─────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["request", "bug"])
def test_both_kinds_are_accepted(kind):
    assert feedback.validate(kind, "The card looks wrong for Bupa Silver Plus.")[0] == kind


def test_an_unknown_kind_is_refused():
    with pytest.raises(feedback.Invalid):
        feedback.validate("complaint", "This is long enough to pass the length check.")


def test_a_message_too_short_to_act_on_is_refused():
    """"broken" is not a report. Better to ask now than to email it on and
    have someone read it tomorrow with nothing to go on."""
    with pytest.raises(feedback.Invalid):
        feedback.validate("bug", "broken")


def test_a_very_long_message_is_kept_but_capped():
    kind, msg = feedback.validate("request", "x" * 9000)
    assert len(msg) == feedback.MAX_MESSAGE


# ── What gets stored ──────────────────────────────────────────────────────

def test_the_context_comes_from_the_server_not_the_message(sent):
    sb = FakeSB()
    feedback.submit(sb, "bug", "The dental limit on this card looks wrong.",
                    user=FakeUser(), site="web", page_key="call_screen",
                    page_url="https://compare.example/card?comp=42&state=NSW",
                    user_agent="Mozilla/5.0 (iPad)")
    row = sb.store["feedback"][0]
    assert row["user_id"] == FakeUser.id
    assert row["user_email"] == FakeUser.email
    assert row["user_name"] == "Pat Rep"
    assert row["page_key"] == "call_screen"
    # The query string is the difference between a report that can be
    # reproduced and one that cannot.
    assert "state=NSW" in row["page_url"]
    assert row["site"] == "web"


def test_an_unknown_site_is_stored_as_nothing_rather_than_a_guess(sent):
    sb = FakeSB()
    feedback.submit(sb, "bug", "Something is wrong on this page somewhere.",
                    user=FakeUser(), site="whatever")
    assert sb.store["feedback"][0]["site"] is None


# ── What gets sent ────────────────────────────────────────────────────────

def test_it_reaches_the_maintainers(sent):
    feedback.submit(FakeSB(), "bug", "The state filter forgets itself on reload.",
                    user=FakeUser(), site="web", page_key="call_screen")
    assert sent[0]["to"] == ["team@example.com"]


def test_replying_to_the_notification_reaches_the_reporter(sent):
    feedback.submit(FakeSB(), "bug", "The state filter forgets itself on reload.",
                    user=FakeUser(), site="web")
    assert sent[0]["reply_to"] == FakeUser.email


def test_the_subject_says_which_kind_and_quotes_the_report(sent):
    feedback.submit(FakeSB(), "request", "Please add a printable version.",
                    user=FakeUser(), site="web")
    assert "Feature request" in sent[0]["subject"]
    assert "printable" in sent[0]["subject"]


def test_the_message_is_escaped_into_the_email(sent):
    """Reports are typed by people and read in an email client. A report about
    a tag is a report, not markup."""
    feedback.submit(FakeSB(), "bug", "The <script> tag on the card page looks wrong.",
                    user=FakeUser(), site="web")
    assert "<script>" not in sent[0]["html"]
    assert "&lt;script&gt;" in sent[0]["html"]


# ── When things fail ──────────────────────────────────────────────────────

def test_a_failed_email_does_not_lose_the_report(monkeypatch):
    monkeypatch.setattr(feedback.mailer, "send",
                        lambda *a, **k: False)
    sb = FakeSB()
    result = feedback.submit(sb, "bug", "Nobody will be emailed about this one.",
                             user=FakeUser(), site="web")
    assert sb.store["feedback"], "the report must survive a failed send"
    assert sb.store["feedback"][0]["emailed"] is False
    assert result["ok"] is True
    # And the person is told, rather than being assured it went somewhere.
    assert "saved" in result["message"]


def test_a_successful_email_is_recorded_against_the_row(sent):
    sb = FakeSB()
    feedback.submit(sb, "bug", "This one does get emailed out properly.",
                    user=FakeUser(), site="web")
    assert sb.store["feedback"][0]["emailed"] is True


def test_a_database_failure_is_not_reported_as_success(sent):
    """Emailing a report that was never stored would tell the person it is on
    a list when it is on no list at all."""
    with pytest.raises(feedback.Invalid):
        feedback.submit(FakeSB(fail=True), "bug", "The database is unreachable just now.",
                        user=FakeUser(), site="web")
    assert sent == []


def test_a_signed_out_caller_still_produces_a_readable_row(sent):
    """Defensive: the endpoints require a session, but a row with no user must
    not crash the report out of existence."""
    sb = FakeSB()
    feedback.submit(sb, "bug", "Filed with no user object attached at all.",
                    user=None, site="web")
    row = sb.store["feedback"][0]
    assert row["user_id"] is None and row["user_email"] is None
