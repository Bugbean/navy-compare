"""The owner's account can't be taken away by another admin.

Deleting the account was never the only way to lose it. Suspending it,
rejecting it, taking its admin rights, setting its password or moving its
email all end in the same place — and the last two hand it to whoever did it.
"""
import pytest

OWNER = "chris.eade@bugbean.com.au"


@pytest.fixture(scope="module")
def ar():
    from app.auth import routes
    return routes


class Admin:
    def __init__(self, email):
        self.email = email
        self.id, self.is_admin = "id-" + email, True


def test_the_owner_is_recognised(ar):
    assert ar._is_owner(OWNER)
    assert ar._is_owner(OWNER.upper())          # case doesn't matter
    assert ar._is_owner("  " + OWNER + " ")     # nor does stray whitespace


def test_someone_else_is_not_the_owner(ar):
    assert not ar._is_owner("someone@else.com")
    assert not ar._is_owner(None)
    assert not ar._is_owner("")


def test_another_admin_cannot_touch_the_owner(ar):
    assert not ar._may_touch_owner(Admin("other@admin.com"), OWNER)


def test_the_owner_can_touch_their_own_account(ar):
    assert ar._may_touch_owner(Admin(OWNER), OWNER)


def test_everyone_else_is_still_editable(ar):
    """This protects one account, not every admin."""
    assert ar._may_touch_owner(Admin("other@admin.com"), "someone@else.com")
    assert ar._may_touch_owner(Admin("other@admin.com"), None)


def test_a_second_owner_can_be_named_without_a_deploy(ar, monkeypatch):
    monkeypatch.setattr(ar, "OWNER_EMAILS", {OWNER, "second@owner.com"})
    assert ar._is_owner("second@owner.com")
    assert not ar._may_touch_owner(Admin("other@admin.com"), "second@owner.com")
    assert ar._may_touch_owner(Admin("second@owner.com"), OWNER)


def test_the_owner_list_is_never_empty_by_accident(ar):
    """An empty list would silently protect nobody."""
    assert ar.OWNER_EMAILS
    assert OWNER in ar.OWNER_EMAILS
