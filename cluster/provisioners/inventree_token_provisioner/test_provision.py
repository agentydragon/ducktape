"""Tests for provision.py's InvenTree token provisioning logic."""

import datetime

import pytest
import pytest_bazel

from cluster.provisioners.inventree_token_provisioner import provision
from cluster.provisioners.inventree_token_provisioner.provision import (
    SANDBOX_USERNAME,
    TOKEN_NAME,
    find_token,
    get_or_create_sandbox_user,
    needs_renewal,
    provision_token,
)


class _FakeInvenTreeUser:
    """InvenTree's model wrapper supports both attribute (u.pk) and dict-style (u["username"]) access."""

    def __init__(self, pk: int, username: str):
        self.pk = pk
        self.username = username

    def __getitem__(self, key: str) -> object:
        return getattr(self, key)


class _FakeApi:
    def __init__(self, get_response: object = None, post_response: dict | None = None):
        self._get_response = get_response
        self._post_response = post_response
        self.deleted: list[str] = []
        self.posted: list[tuple[str, dict]] = []

    def get(self, path: str, params: dict | None = None) -> object:
        return self._get_response

    def post(self, path: str, data: dict) -> dict:
        self.posted.append((path, data))
        assert self._post_response is not None
        return self._post_response

    def delete(self, path: str) -> None:
        self.deleted.append(path)


def test_find_token_returns_matching_named_token_for_user():
    api = _FakeApi(
        get_response=[
            {"user": 1, "name": "other-token", "pk": 10},
            {"user": 2, "name": TOKEN_NAME, "pk": 11},
            {"user": 2, "name": "unrelated", "pk": 12},
        ]
    )
    assert find_token(api, user_pk=2) == {"user": 2, "name": TOKEN_NAME, "pk": 11}


def test_find_token_handles_paginated_dict_response():
    api = _FakeApi(get_response={"results": [{"user": 2, "name": TOKEN_NAME, "pk": 11}]})
    assert find_token(api, user_pk=2) == {"user": 2, "name": TOKEN_NAME, "pk": 11}


def test_find_token_returns_none_when_absent():
    api = _FakeApi(get_response=[])
    assert find_token(api, user_pk=2) is None


def test_needs_renewal_true_when_expiry_soon():
    expiry = (datetime.date.today() + datetime.timedelta(days=10)).isoformat()
    assert needs_renewal({"expiry": expiry}) is True


def test_needs_renewal_false_when_expiry_far_out():
    expiry = (datetime.date.today() + datetime.timedelta(days=60)).isoformat()
    assert needs_renewal({"expiry": expiry}) is False


def test_needs_renewal_false_when_no_expiry():
    assert needs_renewal({}) is False


def test_provision_token_creates_without_revoking_when_no_existing():
    api = _FakeApi(post_response={"token": "new-token-value"})
    token = provision_token(api, user_pk=5, existing=None)
    assert token == "new-token-value"
    assert api.deleted == []
    assert api.posted == [("user/tokens/", {"user": 5, "name": TOKEN_NAME})]


def test_provision_token_revokes_existing_before_creating():
    api = _FakeApi(post_response={"token": "new-token-value"})
    token = provision_token(api, user_pk=5, existing={"pk": 99})
    assert token == "new-token-value"
    assert api.deleted == ["user/tokens/99/"]


def test_provision_token_raises_when_response_missing_token_field():
    api = _FakeApi(post_response={})
    with pytest.raises(RuntimeError, match="missing 'token' field"):
        provision_token(api, user_pk=5, existing=None)


def test_get_or_create_sandbox_user_returns_existing_pk(monkeypatch: pytest.MonkeyPatch):
    existing = _FakeInvenTreeUser(pk=7, username=SANDBOX_USERNAME)
    monkeypatch.setattr(provision.User, "list", lambda api: [existing])
    assert get_or_create_sandbox_user(api=object()) == 7


def test_get_or_create_sandbox_user_creates_when_absent(monkeypatch: pytest.MonkeyPatch):
    created = _FakeInvenTreeUser(pk=9, username=SANDBOX_USERNAME)
    create_calls: list[dict] = []

    def fake_create(api: object, data: dict) -> _FakeInvenTreeUser:
        create_calls.append(data)
        return created

    monkeypatch.setattr(provision.User, "list", lambda api: [])
    monkeypatch.setattr(provision.User, "create", fake_create)

    assert get_or_create_sandbox_user(api=object()) == 9
    assert create_calls == [{"username": SANDBOX_USERNAME}]


if __name__ == "__main__":
    pytest_bazel.main()
