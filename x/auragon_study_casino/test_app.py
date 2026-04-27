"""HTTP-surface tests for the /sync endpoint."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
import pytest_bazel
from fastapi.testclient import TestClient

from x.auragon_study_casino.app import create_app
from x.auragon_study_casino.config import Settings
from x.auragon_study_casino.doc_shape import Casino


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(data_dir=tmp_path, frontend_dist_dir=tmp_path / "nonexistent_dist")
    return TestClient(create_app(settings))


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_pure_pull_returns_seed_state(client: TestClient) -> None:
    """First-time client posts an empty SV + empty update, gets the server's
    full doc back so it can bootstrap."""
    r = client.post("/sync", json={"state_vector_b64": "", "update_b64": ""})
    assert r.status_code == 200
    body = r.json()
    update = _unb64(body["update_b64"])
    casino = Casino.from_update(update)
    assert int(casino.balance["credits"]) == 0
    assert int(casino.balance["tokens"]) == 0
    # default prize catalog seeded
    assert len(casino.prizes) > 0


def test_round_trip_credit_increment(client: TestClient) -> None:
    """Bootstrap, mutate locally, push to server, confirm canonical updated."""
    boot = client.post("/sync", json={"state_vector_b64": "", "update_b64": ""}).json()
    casino = Casino.from_update(_unb64(boot["update_b64"]))
    sv_before = casino.get_state()
    casino.balance["credits"] = 25

    r = client.post(
        "/sync", json={"state_vector_b64": _b64(sv_before), "update_b64": _b64(casino.get_update(sv_before))}
    )
    assert r.status_code == 200, r.text

    # Reload state from server to confirm persistence.
    r2 = client.post("/sync", json={"state_vector_b64": "", "update_b64": ""})
    fresh = Casino.from_update(_unb64(r2.json()["update_b64"]))
    assert int(fresh.balance["credits"]) == 25


def test_negative_credits_rejected_with_409(client: TestClient) -> None:
    boot = client.post("/sync", json={"state_vector_b64": "", "update_b64": ""}).json()
    casino = Casino.from_update(_unb64(boot["update_b64"]))
    sv_before = casino.get_state()
    casino.balance["credits"] = -5

    r = client.post(
        "/sync", json={"state_vector_b64": _b64(sv_before), "update_b64": _b64(casino.get_update(sv_before))}
    )
    assert r.status_code == 409
    body = r.json()
    assert body["rejection"]["rule"] == "credits_nonneg"
    assert "0" in body["rejection"]["message"]

    # Canonical was unchanged.
    fresh = Casino.from_update(
        _unb64(client.post("/sync", json={"state_vector_b64": "", "update_b64": ""}).json()["update_b64"])
    )
    assert int(fresh.balance["credits"]) == 0


def test_invalid_base64_rejected_with_400(client: TestClient) -> None:
    r = client.post("/sync", json={"state_vector_b64": "***not base64***", "update_b64": ""})
    assert r.status_code == 400


def test_two_clients_converge_via_server(client: TestClient) -> None:
    """Phone writes credits=30, syncs. Laptop bootstraps and reads the value."""
    boot = client.post("/sync", json={"state_vector_b64": "", "update_b64": ""}).json()
    phone = Casino.from_update(_unb64(boot["update_b64"]))
    sv_before = phone.get_state()
    phone.balance["credits"] = 30
    client.post("/sync", json={"state_vector_b64": _b64(sv_before), "update_b64": _b64(phone.get_update(sv_before))})

    laptop_boot = client.post("/sync", json={"state_vector_b64": "", "update_b64": ""}).json()
    laptop = Casino.from_update(_unb64(laptop_boot["update_b64"]))
    assert int(laptop.balance["credits"]) == 30


def test_me_returns_default_user_without_oidc(client: TestClient) -> None:
    """Without OIDC config, /me always returns the 'default' user."""
    r = client.get("/me")
    assert r.status_code == 200
    assert r.json() == {"username": "default"}


def test_users_have_isolated_state(tmp_path: Path) -> None:
    """Two different usernames get separate, independent doc states.

    Without OIDC the dep always returns "default", so we exercise isolation
    by using two separate apps with different data_dirs to show isolation
    holds at the DocStore level.
    """
    app_alice = create_app(Settings(data_dir=tmp_path / "alice", frontend_dist_dir=tmp_path / "nonexistent_dist"))
    app_bob = create_app(Settings(data_dir=tmp_path / "bob", frontend_dist_dir=tmp_path / "nonexistent_dist"))
    (tmp_path / "alice").mkdir()
    (tmp_path / "bob").mkdir()

    alice = TestClient(app_alice)
    bob = TestClient(app_bob)

    # Alice earns 50 credits.
    boot = alice.post("/sync", json={"state_vector_b64": "", "update_b64": ""}).json()
    casino = Casino.from_update(_unb64(boot["update_b64"]))
    sv = casino.get_state()
    casino.balance["credits"] = 50
    alice.post("/sync", json={"state_vector_b64": _b64(sv), "update_b64": _b64(casino.get_update(sv))})

    # Bob's state is independent — still at 0.
    boot_bob = bob.post("/sync", json={"state_vector_b64": "", "update_b64": ""}).json()
    bob_casino = Casino.from_update(_unb64(boot_bob["update_b64"]))
    assert int(bob_casino.balance["credits"]) == 0


if __name__ == "__main__":
    pytest_bazel.main()
