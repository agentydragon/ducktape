"""The `hostexec` interface surface: the agent-facing tool input and the `hostexecd` HTTP body.

Three parties share these shapes:
- the **agent** calls the `hostexec_run` MCP tool with `HostexecRunInput`;
- the **console** approves, then token-exchanges the operator's identity for a short-lived,
  per-host Authentik token;
- the in-process console tool POSTs `HostexecRequest` to **`hostexecd`**, which returns a
  `BaseExecResult` (reused from `mcp_infra.exec.models` — the shape every exec backend returns).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from mcp_infra.exec.models import ExecArgsBase

# A POSIX username `hostexecd` may run as. Authorized by the `hostexec-<user>-<host>` Authentik
# group and resolved via `getpwnam` on the host; the charset keeps it a safe group-name component
# and a real account name.
RunAsUser = Annotated[str, Field(pattern=r"^[a-z_][a-z0-9_-]*$", max_length=32)]


class HostexecRunInput(ExecArgsBase):
    """Agent-facing input for the `hostexec_run` MCP tool.

    Inherits the exec fields (`cmd` argv with execve semantics, `max_bytes`, `cwd`, `timeout_ms`)
    from `ExecArgsBase`; adds the target host and the user to run as. Every call is
    approval-gated — this tool is never in `UNCONDITIONAL_AUTO_APPROVE`.
    """

    host: str = Field(description="Target host to run on (e.g. 'wyrm2', 'rugged'). Must be in scope.")
    run_as: RunAsUser = Field(
        description="POSIX username to run as (e.g. 'agentydragon', 'root'). Authorized by the "
        "operator's `hostexec-<user>-<host>` group; 'root' is rendered loudly and needs a second confirm."
    )


class HostexecRequest(BaseModel):
    """In-process console tool → `hostexecd` HTTP body (POST /exec).

    `token` is the operator's short-lived, per-host Authentik token (`aud=hostexec-<host>`,
    carrying the `hostexec-<run_as>-<host>` group), obtained by the console via token exchange on
    approval. `hostexecd` verifies it against Authentik's JWKS, enforces single-use, then runs
    `argv` as `run_as`.
    """

    token: str = Field(description="Operator's per-host Authentik token (aud=hostexec-<host>)")
    run_as: RunAsUser
    argv: list[str] = Field(min_length=1)
    cwd: str | None = None
    max_bytes: int = Field(ge=0, le=100_000)
    timeout_ms: int = Field(gt=0)

    model_config = ConfigDict(extra="forbid")
