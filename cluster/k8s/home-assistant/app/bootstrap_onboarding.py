"""Complete Home Assistant onboarding with a local break-glass owner."""

from __future__ import annotations

import json
import os
import time
from urllib import error, parse, request

BASE_URL = os.environ.get("HOME_ASSISTANT_URL", "http://home-assistant.home-assistant.svc.cluster.local:8123")
CLIENT_ID = "https://home.allegedly.works/"
REDIRECT_URI = CLIENT_ID
USERNAME = "ha-local-admin"
DISPLAY_NAME = "Home Assistant Local Administrator"


def request_json(
    path: str, *, data: dict[str, object] | None = None, token: str | None = None, form: bool = False
) -> object:
    """Send a request to Home Assistant and decode its JSON response."""
    headers = {"Accept": "application/json"}
    body = None
    if data is not None:
        if form:
            body = parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    with request.urlopen(request.Request(f"{BASE_URL}{path}", data=body, headers=headers), timeout=30) as response:
        return json.load(response)


def wait_for_home_assistant() -> None:
    """Wait until the onboarding endpoint is available."""
    for _ in range(60):
        try:
            request_json("/api/onboarding")
            return
        except (error.URLError, TimeoutError):
            time.sleep(5)
    raise TimeoutError("Home Assistant did not become available within 5 minutes")


def onboarding_status() -> set[str]:
    """Return the completed onboarding step names."""
    response = request_json("/api/onboarding")
    if not isinstance(response, list):
        raise TypeError("Home Assistant returned an invalid onboarding status")
    return {
        step["step"]
        for step in response
        if isinstance(step, dict) and step.get("done") is True and isinstance(step.get("step"), str)
    }


def required_string(response: object, *path: str) -> str:
    """Read a required string from a JSON object."""
    value = response
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise ValueError(f"Home Assistant response is missing {'.'.join(path)}")
        value = value[key]
    if not isinstance(value, str):
        raise TypeError(f"Home Assistant response field {'.'.join(path)} is not a string")
    return value


def create_owner(password: str) -> str:
    """Create the local owner and return an authorization code."""
    return required_string(
        request_json(
            "/api/onboarding/users",
            data={
                "name": DISPLAY_NAME,
                "username": USERNAME,
                "password": password,
                "client_id": CLIENT_ID,
                "language": "en",
            },
        ),
        "auth_code",
    )


def login(password: str) -> str:
    """Authenticate the local owner after a partially completed run."""
    flow_id = required_string(
        request_json(
            "/auth/login_flow",
            data={"client_id": CLIENT_ID, "handler": ["homeassistant", None], "redirect_uri": REDIRECT_URI},
        ),
        "flow_id",
    )
    return required_string(
        request_json(
            f"/auth/login_flow/{flow_id}", data={"client_id": CLIENT_ID, "username": USERNAME, "password": password}
        ),
        "result",
    )


def exchange_token(auth_code: str) -> str:
    """Exchange a Home Assistant authorization code for an access token."""
    return required_string(
        request_json(
            "/auth/token",
            data={"grant_type": "authorization_code", "code": auth_code, "client_id": CLIENT_ID},
            form=True,
        ),
        "access_token",
    )


def provision(password: str) -> None:
    """Create the owner if necessary and finish all onboarding steps."""
    wait_for_home_assistant()
    completed = onboarding_status()
    if completed == {"user", "core_config", "integration", "analytics"}:
        print("Home Assistant onboarding is already complete")
        return

    auth_code = login(password) if "user" in completed else create_owner(password)
    token = exchange_token(auth_code)
    if "core_config" not in completed:
        request_json("/api/onboarding/core_config", data={}, token=token)
    if "integration" not in completed:
        request_json(
            "/api/onboarding/integration", data={"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI}, token=token
        )
    if "analytics" not in completed:
        request_json("/api/onboarding/analytics", data={}, token=token)
    print("Home Assistant onboarding is complete")


def main() -> None:
    provision(os.environ["HOME_ASSISTANT_LOCAL_ADMIN_PASSWORD"])


if __name__ == "__main__":
    main()
