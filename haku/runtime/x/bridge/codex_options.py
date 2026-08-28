"""Codex app-server launch material: the exact app-server argv, and the thread params the runner
needs to drive it.

The `CodexHarness` (<codex_harness.py>) owns the JSON-RPC handshake, thread lifecycle, prompts and
projection. This module owns what selects and configures the process: the app-server argv, the
executable variable, and the `thread/start` params — model, reasoning effort, developer
instructions — that the runner now sends and so must receive in the launch.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from tomlkit import document, dumps, inline_table
from tomlkit.items import InlineTable

from haku.runtime.x.bridge.protocol import HarnessLaunch

EXECUTABLE_VARIABLE = "HAKU_CODEX_PATH"

# Codex's `thread/start` params the runner owns now — model, reasoning effort, developer
# instructions — travel to the runner in the launch environment under these keys: the launch argv
# is process config, and these are per-thread. The console writes them (`build_codex_launch`);
# `CodexHarness` reads them (<codex_harness.py>).
CODEX_MODEL_ENV = "HAKU_CODEX_MODEL"
CODEX_REASONING_EFFORT_ENV = "HAKU_CODEX_REASONING_EFFORT"
CODEX_DEVELOPER_INSTRUCTIONS_ENV = "HAKU_CODEX_DEVELOPER_INSTRUCTIONS"


@dataclass(frozen=True, slots=True)
class HttpMcpServer:
    """A streamable-HTTP MCP server Codex reaches with an inherited bearer variable."""

    url: str
    bearer_token_env_var: str | None


@dataclass(frozen=True, slots=True)
class CodexModelProvider:
    """One OpenAI-compatible provider whose secret remains in an inherited variable."""

    provider_id: str
    name: str
    base_url: str
    api_key_env_var: str


@dataclass(frozen=True, slots=True)
class CodexAppServerSession:
    """Everything the Console chooses about one Codex app-server process and its one thread.

    The process fields (`mcp_servers`, `model_provider`) become argv; the thread fields (`model`,
    `reasoning_effort`, `developer_instructions`) become launch-environment keys the runner reads
    for `thread/start`. `reasoning_effort` is a plain string here — the pinned Codex vocabulary
    (`ReasoningEffort`) is validated by the console's deploy config, not re-declared runner-side.
    """

    cwd: Path | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    mcp_servers: Mapping[str, HttpMcpServer] = field(default_factory=dict)
    model_provider: CodexModelProvider | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    developer_instructions: str | None = None


def _toml_inline_table(values: Mapping[str, object]) -> InlineTable:
    """Build an inline TOML table without hand-quoting keys or values."""
    result = inline_table()
    for key, value in values.items():
        if isinstance(value, Mapping):
            result[key] = _toml_inline_table(value)
        else:
            result[key] = value
    return result


def _toml_override(key: str, value: object) -> str:
    """Render one Codex ``-c`` override as a complete, parseable TOML assignment."""
    config = document()
    config[key] = value
    return dumps(config).strip()


def _mcp_config(servers: Mapping[str, tuple[str, str | None]]) -> str:
    entries: dict[str, dict[str, str]] = {}
    for name, (url, bearer_token_env_var) in sorted(servers.items()):
        entry = {"url": url}
        if bearer_token_env_var is not None:
            entry["bearer_token_env_var"] = bearer_token_env_var
        entries[name] = entry
    return _toml_override("mcp_servers", _toml_inline_table(entries))


def _model_provider_config(provider: CodexModelProvider) -> str:
    return _toml_override(
        "model_providers",
        _toml_inline_table(
            {
                provider.provider_id: {
                    "name": provider.name,
                    "base_url": provider.base_url,
                    "env_key": provider.api_key_env_var,
                    "wire_api": "responses",
                }
            }
        ),
    )


def build_codex_launch(session: CodexAppServerSession, *, resume_from: int | None = None) -> HarnessLaunch:
    """Launch the pinned Codex binary as one newline-delimited stdio app-server."""
    arguments: list[str] = []
    if session.model_provider is not None:
        # app-server intentionally disables first-party OPENAI_API_KEY auth. A named provider's
        # env_key is the supported programmatic path: only the variable name enters argv while
        # the SandboxTemplate-owned credential contract remains in the child environment.
        arguments.extend(
            (
                "-c",
                _toml_override("model_provider", session.model_provider.provider_id),
                "-c",
                _model_provider_config(session.model_provider),
            )
        )
    if session.mcp_servers:
        arguments.extend(
            (
                "-c",
                _mcp_config(
                    {name: (server.url, server.bearer_token_env_var) for name, server in session.mcp_servers.items()}
                ),
            )
        )
    arguments.extend(("app-server", "--listen", "stdio://"))
    return HarnessLaunch(
        arguments=tuple(arguments),
        cwd=str(session.cwd) if session.cwd is not None else ".",
        environment=_launch_environment(session),
        resume_from=resume_from,
    )


def _launch_environment(session: CodexAppServerSession) -> dict[str, str]:
    """The child environment: the console's, plus the thread params the runner reads for
    `thread/start` (<codex_harness.py>). A thread param that is None is simply absent — Codex then
    falls back to its configured default."""
    thread_params = {
        CODEX_MODEL_ENV: session.model,
        CODEX_REASONING_EFFORT_ENV: session.reasoning_effort,
        CODEX_DEVELOPER_INSTRUCTIONS_ENV: session.developer_instructions,
    }
    return {**session.environment, **{key: value for key, value in thread_params.items() if value is not None}}
