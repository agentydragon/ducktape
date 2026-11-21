import json
import sys
from io import BytesIO
from unittest.mock import Mock

import pytest
import pytest_bazel

from cluster.scripts import check_authentik_login


@pytest.mark.parametrize(
    ("direct_url", "mfa_required"),
    [
        pytest.param(False, False, id="default"),
        pytest.param(True, False, id="overrides"),
        pytest.param(False, True, id="mfa-required"),
    ],
)
def test_password_login_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], direct_url: bool, mfa_required: bool
) -> None:
    if direct_url:
        base, flow, user = "https://auth.example.test", "custom-flow", "admin"
        secret_ns, secret_name, secret_key = "test", "admin-auth", "TEST_PASSWORD"
        flags = [
            "--url",
            base + "/",
            "--flow-slug",
            flow,
            "--user",
            user,
            "--secret-ns",
            secret_ns,
            "--secret-name",
            secret_name,
            "--secret-key",
            secret_key,
        ]
    else:
        base, flow, user = "http://localhost:19080", "default-authentication-flow", "agentydragon"
        secret_ns, secret_name, secret_key = "authentik", "authentik-user-password", "USER_PASSWORD"
        flags = []

    monkeypatch.setattr(sys, "argv", ["check_authentik_login", *flags])
    kubectl = Mock(return_value=Mock(stdout="dGVzdC1wYXNzd29yZA=="))
    monkeypatch.setattr(check_authentik_login.subprocess, "run", kubectl)
    forward = Mock()
    monkeypatch.setattr(check_authentik_login, "start_port_forward", forward)
    opener = Mock()
    final_component = "ak-stage-authenticator-validate" if mfa_required else "xak-flow-redirect"
    opener.open.side_effect = [
        BytesIO(json.dumps({"component": component}).encode())
        for component in ("ak-stage-identification", "ak-stage-password", final_component)
    ]
    build_opener = Mock(return_value=opener)
    monkeypatch.setattr(check_authentik_login.urllib.request, "build_opener", build_opener)

    if mfa_required:
        with pytest.raises(SystemExit) as exited:
            check_authentik_login.main()
        assert exited.value.code == 1
        output = capsys.readouterr().out
        assert f"Expected xak-flow-redirect, got {final_component}" in output
        assert "test-password" not in output
    else:
        check_authentik_login.main()

    build_opener.assert_called_once()
    assert kubectl.call_args.args[0] == [
        "kubectl",
        "get",
        "secret",
        "-n",
        secret_ns,
        secret_name,
        "-o",
        f"jsonpath={{.data.{secret_key}}}",
    ]
    requests = [call.args[0] for call in opener.open.call_args_list]
    assert [req.full_url for req in requests] == [f"{base}/api/v3/flows/executor/{flow}/"] * 3
    assert [req.get_method() for req in requests] == ["GET", "POST", "POST"]
    assert [json.loads(req.data) for req in requests[1:]] == [{"uid_field": user}, {"password": "test-password"}]
    if direct_url:
        forward.assert_not_called()
    else:
        forward.assert_called_once_with("authentik", "authentik-server", 19080, 80)
        forward.return_value.terminate.assert_called_once()
        forward.return_value.wait.assert_called_once()


if __name__ == "__main__":
    pytest_bazel.main()
