import bootstrap_onboarding
import pytest_bazel


def test_fresh_install_creates_owner_and_completes_onboarding(monkeypatch):
    calls: list[tuple[str, dict[str, object] | None, str | None, bool]] = []

    def fake_request(
        path: str, *, data: dict[str, object] | None = None, token: str | None = None, form: bool = False
    ) -> object:
        calls.append((path, data, token, form))
        if path == "/api/onboarding":
            return [{"step": "user", "done": False}]
        if path == "/api/onboarding/users":
            return {"auth_code": "owner-code"}
        if path == "/auth/token":
            return {"access_token": "bootstrap-token"}
        return {}

    monkeypatch.setattr(bootstrap_onboarding, "request_json", fake_request)
    bootstrap_onboarding.provision("secret-password")

    assert calls == [
        ("/api/onboarding", None, None, False),
        ("/api/onboarding", None, None, False),
        (
            "/api/onboarding/users",
            {
                "name": bootstrap_onboarding.DISPLAY_NAME,
                "username": bootstrap_onboarding.USERNAME,
                "password": "secret-password",
                "client_id": bootstrap_onboarding.CLIENT_ID,
                "language": "en",
            },
            None,
            False,
        ),
        (
            "/auth/token",
            {"grant_type": "authorization_code", "code": "owner-code", "client_id": bootstrap_onboarding.CLIENT_ID},
            None,
            True,
        ),
        ("/api/onboarding/core_config", {}, "bootstrap-token", False),
        (
            "/api/onboarding/integration",
            {"client_id": bootstrap_onboarding.CLIENT_ID, "redirect_uri": bootstrap_onboarding.REDIRECT_URI},
            "bootstrap-token",
            False,
        ),
        ("/api/onboarding/analytics", {}, "bootstrap-token", False),
    ]


def test_partial_run_logs_in_and_finishes_remaining_steps(monkeypatch):
    calls: list[str] = []

    def fake_request(
        path: str, *, data: dict[str, object] | None = None, token: str | None = None, form: bool = False
    ) -> object:
        calls.append(path)
        if path == "/api/onboarding":
            return [
                {"step": "user", "done": True},
                {"step": "core_config", "done": True},
                {"step": "integration", "done": False},
                {"step": "analytics", "done": False},
            ]
        if path == "/auth/login_flow":
            return {"flow_id": "login-flow"}
        if path == "/auth/login_flow/login-flow":
            return {"result": "login-code"}
        if path == "/auth/token":
            return {"access_token": "bootstrap-token"}
        return {}

    monkeypatch.setattr(bootstrap_onboarding, "request_json", fake_request)
    bootstrap_onboarding.provision("secret-password")

    assert calls == [
        "/api/onboarding",
        "/api/onboarding",
        "/auth/login_flow",
        "/auth/login_flow/login-flow",
        "/auth/token",
        "/api/onboarding/integration",
        "/api/onboarding/analytics",
    ]


def test_completed_onboarding_is_a_noop(monkeypatch):
    calls = 0

    def fake_request(
        path: str, *, data: dict[str, object] | None = None, token: str | None = None, form: bool = False
    ) -> object:
        nonlocal calls
        calls += 1
        return [
            {"step": "user", "done": True},
            {"step": "core_config", "done": True},
            {"step": "integration", "done": True},
            {"step": "analytics", "done": True},
        ]

    monkeypatch.setattr(bootstrap_onboarding, "request_json", fake_request)
    bootstrap_onboarding.provision("secret-password")

    assert calls == 2


if __name__ == "__main__":
    pytest_bazel.main()
