"""The `hostexec` interface surface: the agent-facing tool input and the `hostexecd` HTTP body.

Three parties share these shapes:
- the **agent** calls the MCP tool with `HostexecRunInput`;
- the **console** approves, then mints a `SignedCapability` and forwards the operator's Authentik
  token;
- `hostexec-mcp` POSTs `HostexecRequest` to **`hostexecd`**, which returns a `BaseExecResult`
  (reused from `mcp_infra.exec.models` — same shape every exec backend returns).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from haku.hostexec.capability import RunAs, SignedCapability
from mcp_infra.exec.models import ExecArgsBase


class HostexecRunInput(ExecArgsBase):
    """Agent-facing input for the `hostexec_run` MCP tool.

    Inherits the exec fields (`cmd` argv with execve semantics, `max_bytes`, `cwd`, `timeout_ms`)
    from `ExecArgsBase`; adds the target host and the user to run as. Every call is
    approval-gated — this tool is never in `UNCONDITIONAL_AUTO_APPROVE`.
    """

    host: str = Field(description="Target host to run on (e.g. 'wyrm2', 'rugged'). Must be in scope.")
    run_as: RunAs = Field(
        description="POSIX user to run as: 'agentydragon' (unprivileged) or 'root' (privileged). "
        "Both require operator approval; 'root' is rendered loudly and requires a second confirm."
    )


class HostexecRequest(BaseModel):
    """`hostexec-mcp` → `hostexecd` HTTP body (POST /exec).

    `token` is the approving operator's forwarded Authentik JWT (carries the revocable
    `hostexec-<run_as>-<host>` authorization). `capability` is the console countersignature
    binding this exact command. `hostexecd` requires **both**: the token authorizes the identity,
    the capability binds the command. `run_as`/`argv`/`cwd` are what actually gets executed;
    `hostexecd` cross-checks them against the capability before running.
    """

    token: str = Field(description="Forwarded operator Authentik JWT (aud=hostexec)")
    capability: SignedCapability
    run_as: RunAs
    argv: list[str] = Field(min_length=1)
    cwd: str | None = None
    max_bytes: int = Field(ge=0, le=100_000)
    timeout_ms: int = Field(gt=0)

    model_config = ConfigDict(extra="forbid")
