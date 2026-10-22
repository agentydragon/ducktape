"""Reconciler behavior against a mocked Grocy API: auto-creation via the
reverse-proxy header, string-typed SQLite ids, PUT only on drift."""

import json

import httpx
import pytest

from cluster.provisioners.grocy_user_perms.provision import Policy, reconcile

PERMISSION_HIERARCHY = [{"id": "1", "name": "ADMIN"}, {"id": "2", "name": "MASTER_DATA_EDIT"}]


class FakeGrocy:
    """Minimal Grocy API double; numeric ids serialize as strings like the real one."""

    def __init__(self, user_permissions: dict[str, set[int]]) -> None:
        self.user_permissions = user_permissions
        self.puts: dict[str, list[int]] = {}

    def _user_ids(self) -> dict[str, int]:
        return {name: i + 1 for i, name in enumerate(self.user_permissions)}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/system/info":
            # Reverse-proxy auth auto-creates the requesting user.
            self.user_permissions.setdefault(request.headers["X-authentik-username"], set())
            return httpx.Response(200, json={})
        if path == "/api/objects/permission_hierarchy":
            return httpx.Response(200, json=PERMISSION_HIERARCHY)
        if path == "/api/users":
            return httpx.Response(
                200, json=[{"id": str(uid), "username": name} for name, uid in self._user_ids().items()]
            )
        username = {uid: name for name, uid in self._user_ids().items()}[int(path.split("/")[3])]
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {"id": "99", "user_id": path.split("/")[3], "permission_id": str(p)}
                    for p in sorted(self.user_permissions[username])
                ],
            )
        assert request.method == "PUT"
        granted = json.loads(request.content)["permissions"]
        self.puts[username] = granted
        self.user_permissions[username] = set(granted)
        return httpx.Response(204)


def make_client(fake: FakeGrocy) -> httpx.Client:
    return httpx.Client(base_url="http://grocy", transport=httpx.MockTransport(fake.handler))


def test_converges_only_drifted_users():
    # test-admin already at its full explicit set (no PUT), test-user wrongly elevated
    # (PUT back to empty), test-operator absent (auto-created, then PUT to its full set).
    fake = FakeGrocy({"test-admin": {1, 2}, "test-user": {2}})
    reconcile(
        make_client(fake),
        Policy(
            users={
                "test-admin": {"ADMIN", "MASTER_DATA_EDIT"},
                "test-operator": {"ADMIN", "MASTER_DATA_EDIT"},
                "test-user": set(),
            }
        ),
    )
    assert fake.puts == {"test-operator": [1, 2], "test-user": []}
    assert fake.user_permissions == {"test-admin": {1, 2}, "test-operator": {1, 2}, "test-user": set()}


def test_unlisted_users_untouched():
    fake = FakeGrocy({"test-admin": {1, 2}, "test-bystander": {2}})
    reconcile(make_client(fake), Policy(users={"test-admin": {"ADMIN", "MASTER_DATA_EDIT"}}))
    assert fake.puts == {}
    assert fake.user_permissions["test-bystander"] == {2}


def test_unknown_permission_name_rejected_before_any_write():
    fake = FakeGrocy({"test-admin": {1}, "test-user": {2}})
    with pytest.raises(ValueError, match="NOT_A_PERMISSION"):
        reconcile(make_client(fake), Policy(users={"test-admin": {"NOT_A_PERMISSION"}, "test-user": set()}))
    assert fake.puts == {}
