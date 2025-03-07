import pytest

from middlewared.test.integration.assets.account import unprivileged_user_client


@pytest.mark.parametrize("role", ["READONLY", "FULL_ADMIN"])
def test_user_role_in_account(role):
    with unprivileged_user_client(roles=[role]) as c:
        this_user = c.call("user.query", [["username", "=", c.username]], {"get": True})

        assert this_user['roles'] == [role]


def test_user_role_full_admin_map():
    with unprivileged_user_client(allowlist=[{"method": "*", "resource": "*"}]) as c:
        this_user = c.call("user.query", [["username", "=", c.username]], {"get": True})

        assert "FULL_ADMIN" in this_user["roles"]
        assert "HAS_ALLOW_LIST" in this_user["roles"]
