"""Codex app-server as a bridge backend: native process launch and executable resolution.

The Console-side adapter owns the JSON-RPC handshake, thread configuration, prompts and
projection. This module owns only what the shared runner needs: the exact app-server argv and the
binary that answers it. Native messages remain opaque ``HarnessFrame`` payloads to the runner.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from tomlkit import document, dumps, inline_table
from tomlkit.items import InlineTable

from haku.runtime.x.bridge.backend import HarnessDriver, ProcessLaunch, child_environment
from haku.runtime.x.bridge.protocol import HarnessLaunch

EXECUTABLE_VARIABLE = "HAKU_CODEX_PATH"


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
    """Everything the Console chooses about one Codex app-server process."""

    cwd: Path | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    mcp_servers: Mapping[str, HttpMcpServer] = field(default_factory=dict)
    model_provider: CodexModelProvider | None = None


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
        environment=dict(session.environment),
        resume_from=resume_from,
    )


@dataclass(frozen=True, slots=True)
class CodexAppServerBackend:
    """Codex app-server, as the sandbox runner starts it and reads it back."""

    name: ClassVar[str] = "codex-app-server"
    executable: Path

    def resolve(self, launch: HarnessLaunch) -> ProcessLaunch:
        return ProcessLaunch(
            executable=self.executable,
            arguments=launch.arguments,
            cwd=launch.cwd,
            environment=child_environment(launch),
        )

    def driver(self) -> HarnessDriver:
        # CLEANUP(added 2026-08-27): implement with the Codex runner-side projector (#4667
        # stage 5) and delete this refusal.
        raise NotImplementedError(
            "codex-app-server is not yet ported to the neutral-operation generation (#4667 stage 5)"
        )


def codex_app_server_backend(executable: Path | None = None) -> CodexAppServerBackend:
    """Codex at the image-selected path, or at *executable* for a test/local run."""
    return CodexAppServerBackend(
        executable=executable if executable is not None else Path(os.environ.get(EXECUTABLE_VARIABLE, "codex"))
    )
