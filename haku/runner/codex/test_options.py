"""The exact Codex app-server process launch selected by its provider adapter."""

import tomllib
from pathlib import Path

import pytest
import pytest_bazel

from haku.runner.backend_registry import runner_harnesses
from haku.runner.codex.harness import codex_harness
from haku.runner.codex.options import CodexAppServerSession, CodexModelProvider, HttpMcpServer, build_codex_launch


def test_the_app_server_launch_is_exactly_this() -> None:
    launch = build_codex_launch(
        CodexAppServerSession(
            cwd=Path("/workspace"),
            environment={"CODEX_HOME": "/codex-home"},
            model_provider=CodexModelProvider(
                provider_id="haku",
                name="Haku OpenAI-compatible",
                base_url="http://litellm.test/v1",
                api_key_env_var="OPENAI_API_KEY",
            ),
            mcp_servers={
                "haku-console": HttpMcpServer(url="https://console.test/mcp", bearer_token_env_var="HAKU_RUNNER_TOKEN")
            },
        ),
        resume_from=19,
    )

    assert launch.arguments == (
        "-c",
        'model_provider = "haku"',
        "-c",
        'model_providers = {haku = {name = "Haku OpenAI-compatible", '
        'base_url = "http://litellm.test/v1", env_key = "OPENAI_API_KEY", '
        'wire_api = "responses"}}',
        "-c",
        'mcp_servers = {haku-console = {url = "https://console.test/mcp", bearer_token_env_var = "HAKU_RUNNER_TOKEN"}}',
        "app-server",
        "--listen",
        "stdio://",
    )
    assert launch.cwd == "/workspace"
    assert launch.environment == {"CODEX_HOME": "/codex-home"}
    assert launch.resume_from == 19


def test_the_provider_credential_never_enters_codex_arguments() -> None:
    launch = build_codex_launch(
        CodexAppServerSession(
            environment={"OPENAI_API_KEY": "provider-secret"},
            model_provider=CodexModelProvider(
                provider_id="haku",
                name="Haku OpenAI-compatible",
                base_url="http://litellm.test/v1",
                api_key_env_var="OPENAI_API_KEY",
            ),
        )
    )

    assert "provider-secret" not in " ".join(launch.arguments)
    assert launch.environment["OPENAI_API_KEY"] == "provider-secret"


def test_the_backend_preserves_the_claim_owned_session_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAKU_RUNNER_TOKEN", "session-bearer")
    launch = build_codex_launch(
        CodexAppServerSession(
            environment={"HAKU_RUNNER_TOKEN": "injected-secret", "SAFE": "value"},
            mcp_servers={
                "haku-console": HttpMcpServer(url="https://console.test/mcp", bearer_token_env_var="HAKU_RUNNER_TOKEN")
            },
        )
    )

    resolved = codex_harness(Path("/usr/local/bin/codex")).resolve(launch)

    assert resolved.command == [
        "/usr/local/bin/codex",
        "-c",
        'mcp_servers = {haku-console = {url = "https://console.test/mcp", bearer_token_env_var = "HAKU_RUNNER_TOKEN"}}',
        "app-server",
        "--listen",
        "stdio://",
    ]
    assert resolved.environment["HAKU_RUNNER_TOKEN"] == "session-bearer"
    assert resolved.environment["SAFE"] == "value"


def test_the_backend_only_resolves_the_binary_without_mcp() -> None:
    launch = build_codex_launch(CodexAppServerSession())
    resolved = codex_harness(Path("/usr/local/bin/codex")).resolve(launch)

    assert resolved.command == ["/usr/local/bin/codex", "app-server", "--listen", "stdio://"]
    assert resolved.cwd == "."


def test_the_shared_runner_links_the_codex_backend_without_a_provider_branch() -> None:
    assert "codex-app-server" in runner_harnesses()


def test_structured_overrides_round_trip_toml_edge_case_values() -> None:
    provider = CodexModelProvider(
        provider_id='provider."punctuation"',
        name='name with "quotes", \\slashes\\,\nnew line, and unicode ✓',
        base_url="https://example.test/v1?x=1&y=2#fragment",
        api_key_env_var="PROVIDER_KEY_ENV",
    )
    launch = build_codex_launch(
        CodexAppServerSession(
            model_provider=provider,
            mcp_servers={
                'mcp."one"': HttpMcpServer(
                    url='https://one.test/mcp?query="quoted"\\path', bearer_token_env_var="MCP_ONE_TOKEN"
                ),
                "mcp-two": HttpMcpServer(url="https://two.test/mcp\nwith-newline", bearer_token_env_var=None),
            },
        )
    )

    overrides = [launch.arguments[index + 1] for index, argument in enumerate(launch.arguments) if argument == "-c"]
    parsed = [tomllib.loads(override) for override in overrides]

    assert parsed == [
        {"model_provider": provider.provider_id},
        {
            "model_providers": {
                provider.provider_id: {
                    "name": provider.name,
                    "base_url": provider.base_url,
                    "env_key": provider.api_key_env_var,
                    "wire_api": "responses",
                }
            }
        },
        {
            "mcp_servers": {
                "mcp-two": {"url": "https://two.test/mcp\nwith-newline"},
                'mcp."one"': {
                    "url": 'https://one.test/mcp?query="quoted"\\path',
                    "bearer_token_env_var": "MCP_ONE_TOKEN",
                },
            }
        },
    ]


if __name__ == "__main__":
    pytest_bazel.main()
