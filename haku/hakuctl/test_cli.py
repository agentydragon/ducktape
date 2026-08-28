import contextlib

import pytest
import pytest_bazel
import typer
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport
from typer.testing import CliRunner

from haku.hakuctl.cli import TOKEN_ENV, _client, app, build_client

runner = CliRunner()


def _all_output(result) -> str:
    parts = [result.output or ""]
    with contextlib.suppress(ValueError):
        parts.append(result.stderr or "")
    return "".join(parts)


def test_build_client_uses_streamable_http_transport() -> None:
    client = build_client("https://example.test/mcp", "tok-123")
    assert isinstance(client, Client)
    assert isinstance(client.transport, StreamableHttpTransport)


def test_client_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    with pytest.raises(typer.Exit) as exc:
        _client("https://example.test/mcp")
    assert exc.value.exit_code == 2


def test_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("list", "schema", "call"):
        assert command in result.output


def test_call_rejects_non_object_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, "tok")
    result = runner.invoke(app, ["call", "some_tool", "[]"])
    assert result.exit_code == 2
    assert "JSON object" in _all_output(result)


if __name__ == "__main__":
    pytest_bazel.main()
