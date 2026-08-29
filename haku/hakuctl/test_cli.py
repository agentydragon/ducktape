import base64
import contextlib
import subprocess
from unittest.mock import MagicMock

import pytest
import pytest_bazel
import typer
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport
from typer.testing import CliRunner

from haku.hakuctl import cli

runner = CliRunner()


def _all_output(result) -> str:
    parts = [result.output or ""]
    with contextlib.suppress(ValueError):
        parts.append(result.stderr or "")
    return "".join(parts)


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["kubectl"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_build_client_uses_streamable_http_transport() -> None:
    client = cli.build_client("https://example.test/mcp", "tok-123")
    assert isinstance(client, Client)
    assert isinstance(client.transport, StreamableHttpTransport)


def test_help_lists_subcommands() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in ("list", "schema", "call"):
        assert command in result.output


def test_call_rejects_non_object_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cli.TOKEN_ENV, "tok")
    result = runner.invoke(cli.app, ["call", "some_tool", "[]"])
    assert result.exit_code == 2
    assert "JSON object" in _all_output(result)


def test_env_token_wins_and_kubectl_is_not_consulted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cli.TOKEN_ENV, "env-tok")
    run = MagicMock()
    monkeypatch.setattr(cli.subprocess, "run", run)
    client = cli._client("https://example.test/mcp")
    assert isinstance(client, Client)
    run.assert_not_called()


def test_falls_back_to_kubernetes_secret_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cli.TOKEN_ENV, raising=False)
    encoded = base64.b64encode(b"agent-bearer-xyz").decode()

    def _fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(0, stdout=encoded)

    monkeypatch.setattr(cli.subprocess, "run", _fake_run)

    captured: dict[str, str] = {}

    def _spy(url: str, token: str) -> None:
        captured["url"] = url
        captured["token"] = token

    monkeypatch.setattr(cli, "build_client", _spy)

    cli._client("https://example.test/mcp")
    # The base64 secret is decoded before it reaches the client, not passed through raw.
    assert captured == {"url": "https://example.test/mcp", "token": "agent-bearer-xyz"}


def test_errors_cleanly_when_kubectl_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cli.TOKEN_ENV, raising=False)

    def _missing(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("kubectl")

    monkeypatch.setattr(cli.subprocess, "run", _missing)
    with pytest.raises(typer.Exit) as exc:
        cli._client("https://example.test/mcp")
    assert exc.value.exit_code == 2


def test_kubectl_failure_warns_loudly_and_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(cli.TOKEN_ENV, raising=False)
    monkeypatch.setenv("COLUMNS", "200")  # keep the Rich warning on one line so the reason isn't wrapped mid-token
    stderr = 'Error from server (Forbidden): secrets "haku-console-agent-api" is forbidden'

    def _forbidden(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(1, stderr=stderr)

    monkeypatch.setattr(cli.subprocess, "run", _forbidden)

    with pytest.raises(typer.Exit) as exc:
        cli._client("https://example.test/mcp")
    assert exc.value.exit_code == 2
    warned = capsys.readouterr().err
    # Degrade loud: the failure names the secret it tried and echoes kubectl's reason.
    assert f"{cli._TOKEN_SECRET_NAMESPACE}/{cli._TOKEN_SECRET_NAME}" in warned
    assert "Forbidden" in warned


if __name__ == "__main__":
    pytest_bazel.main()
