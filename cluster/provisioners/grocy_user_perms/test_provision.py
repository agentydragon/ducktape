"""Reconciler behavior against a mocked Grocy API: auto-creation via the
reverse-proxy header, string-typed SQLite ids, PUT only on drift."""

import json

import httpx
import pytest
import pytest_bazel

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


def test_admin_expands_to_every_known_permission():
    # "ADMIN" doesn't just grant the ADMIN row -- Grocy's own runtime permission check
    # does an exact-name lookup (via user_permissions_resolved), not hierarchy resolution,
    # so ADMIN must expand to literally every permission_hierarchy id to mean "everything".
    # agentydragon holds only the ADMIN row (drift, PUT to the full set), haku wrongly
    # elevated (PUT back to empty), auragon absent (auto-created, then PUT to the full set).
    fake = FakeGrocy({"agentydragon": {1}, "haku": {2}})
    reconcile(make_client(fake), Policy(users={"agentydragon": {"ADMIN"}, "auragon": {"ADMIN"}, "haku": set()}))
    assert fake.puts == {"agentydragon": [1, 2], "auragon": [1, 2], "haku": []}
    assert fake.user_permissions == {"agentydragon": {1, 2}, "auragon": {1, 2}, "haku": set()}


def test_admin_user_already_at_full_set_is_not_repeatedly_put():
    fake = FakeGrocy({"agentydragon": {1, 2}})
    reconcile(make_client(fake), Policy(users={"agentydragon": {"ADMIN"}}))
    assert fake.puts == {}
    assert fake.user_permissions["agentydragon"] == {1, 2}


def test_unlisted_users_untouched():
    fake = FakeGrocy({"agentydragon": {1, 2}, "bystander": {2}})
    reconcile(make_client(fake), Policy(users={"agentydragon": {"ADMIN"}}))
    assert fake.puts == {}
    assert fake.user_permissions["bystander"] == {2}


def test_unknown_permission_name_rejected_before_any_write():
    fake = FakeGrocy({"agentydragon": {1}, "haku": {2}})
    with pytest.raises(ValueError, match="NOT_A_PERMISSION"):
        reconcile(make_client(fake), Policy(users={"agentydragon": {"NOT_A_PERMISSION"}, "haku": set()}))
    assert fake.puts == {}


if __name__ == "__main__":
    pytest_bazel.main()
