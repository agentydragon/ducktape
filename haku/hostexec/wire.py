"""The `hostexec` wire body between the in-process console tool and `hostexecd`.

Three parties share these shapes:
- the **agent** calls the `hostexec_run` MCP tool (agent-facing input is the tool's own flat
  signature in `haku/console/tools/hostexec.py`; `RunAsUser` below is shared with it);
- the **console** approves, then token-exchanges the operator's identity for a short-lived,
  per-host Authentik token;
- the console durably queues `HostexecRequest`; **`hostexecd`** claims it over its outbound session
  and submits a `BaseExecResult` (reused from `mcp_infra.exec.models`).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

# A POSIX username `hostexecd` may run as. Authorized by the `hostexec-<user>-<host>` Authentik
# group and resolved via `getpwnam` on the host; the charset keeps it a safe group-name component
# and a real account name.
RunAsUser = Annotated[str, Field(pattern=r"^[a-z_][a-z0-9_-]*$", max_length=32)]


class HostexecRequest(BaseModel):
    """Approved command body claimed by `hostexecd` from the console broker.

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
