"""In-process MCP tools for the one Kubernetes Agent Sandbox environment Console hands out."""

from __future__ import annotations

from typing import Annotated, Protocol

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from haku.sandbox.config import SandboxEnvironmentConfig
from haku.sandbox.models import DisposeSandboxResult, SandboxExecResult, SandboxInfo, SandboxListPage, SandboxName

SANDBOX_SERVER_ID = "sandbox"
_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_PROVISION = ToolAnnotations(destructiveHint=False, idempotentHint=True, openWorldHint=False)
_DISPOSE = ToolAnnotations(destructiveHint=True, idempotentHint=True, openWorldHint=False)
_SCRIPT = Annotated[
    str,
    Field(
        min_length=1,
        description=(
            "Bash script text executed with `bash -lc`. Pipes, redirects, globs, variable "
            "expansion, and compound commands use normal Bash semantics."
        ),
    ),
]
_TIMEOUT_SECONDS = Annotated[int, Field(gt=0, le=3600, description="Command timeout in seconds.")]
_MAX_OUTPUT_BYTES = Annotated[
    int, Field(ge=0, le=1_000_000, description="Maximum bytes retained independently from stdout and stderr.")
]
_CWD = Annotated[
    str | None,
    Field(default=None, description="Working directory inside the sandbox; omit to use the configured default."),
]


class SandboxClient(Protocol):
    async def provision(self, name: str) -> SandboxInfo: ...

    async def execute(
        self, *, name: str, script: str, cwd: str | None, timeout_seconds: int, max_output_bytes: int
    ) -> SandboxExecResult: ...

    async def info(self, name: str) -> SandboxInfo: ...

    async def list(self, *, limit: int, continue_token: str | None) -> SandboxListPage: ...

    async def dispose(self, name: str) -> DisposeSandboxResult: ...

    async def aclose(self) -> None: ...


def build_mcp(client: SandboxClient, environment: SandboxEnvironmentConfig) -> FastMCP:
    """Build the agent-facing five-tool surface around an injected lifecycle client."""

    mcp: FastMCP = FastMCP(
        name=SANDBOX_SERVER_ID,
        strict_input_validation=True,
        instructions=(
            "Provision and use the one configured Kubernetes Agent Sandbox environment. "
            "Provisioning creates or resumes a named claim and runs its reviewed bootstrap. "
            "Every exec refreshes the sandbox deadline before running a bounded Bash script."
        ),
    )

    @mcp.tool(annotations=_PROVISION)
    async def provision_sandbox(name: SandboxName) -> SandboxInfo:
        """Create or resume a named sandbox and run its configured bootstrap.

        Use this before executing work. The call waits for Agent Sandbox allocation, Pod readiness,
        and bootstrap, but may return ``state=provisioning`` or ``state=ready`` with
        ``bootstrap_state=running`` if the configured wait budget expires; call
        ``get_sandbox_info`` or retry this idempotent tool. A same-named foreign claim is rejected.
        An existing claim whose recorded environment no longer matches the current configuration is
        still resumed; the mismatch is reported in ``warnings`` for you to weigh against the work at
        hand, and ``dispose_sandbox`` is how you act on it.
        """

        return await client.provision(name)

    async def exec_sandbox(
        name: SandboxName,
        script: _SCRIPT,
        timeout_seconds: _TIMEOUT_SECONDS,
        max_output_bytes: _MAX_OUTPUT_BYTES,
        cwd: _CWD = None,
    ) -> SandboxExecResult:
        """Run one bounded Bash script in a ready sandbox.

        Use this after ``provision_sandbox`` reports ``state=ready`` and
        ``bootstrap_state=succeeded``, or to diagnose ``bootstrap_state=failed``. Before starting
        the command the server confirms at least the configured TTL extension remains; if renewal
        fails, no command runs. The result includes exit status, bounded stdout/stderr, duration in
        seconds, and the confirmed deadline. A nonzero exit is a normal result, not an MCP
        transport error.
        """

        return await client.execute(
            name=name, script=script, cwd=cwd, timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes
        )

    # Registered via add_tool rather than the @mcp.tool decorator so the returned Tool's
    # advertised schema can be patched: _TIMEOUT_SECONDS/_MAX_OUTPUT_BYTES's le= bounds are
    # fixed outer ceilings baked in at module-def time, since FastMCP derives Field bounds from
    # the plain function signature and has no way to see this environment's configured maxes
    # there. Those ceilings are still enforced independently by kubernetes_client.execute();
    # this only fixes what the tool schema advertises to callers.
    exec_tool = mcp.add_tool(exec_sandbox)
    exec_tool.parameters["properties"]["timeout_seconds"]["maximum"] = environment.sandbox.max_exec_timeout_seconds
    exec_tool.parameters["properties"]["max_output_bytes"]["maximum"] = environment.sandbox.max_output_bytes

    @mcp.tool(annotations=_READ_ONLY)
    async def get_sandbox_info(name: SandboxName) -> SandboxInfo:
        """Inspect one managed sandbox without changing it.

        Use this to poll provisioning, diagnose bootstrap failure, or confirm readiness and expiry
        before more work. The response combines claim, assigned Sandbox, Pod, target-container, and
        bootstrap state without exposing raw manifests, environment variables, or secrets. Missing
        and foreign claims are actionable MCP errors. ``warnings`` names each way the running
        sandbox no longer matches the current configuration — a different bootstrap script, warm
        pool, container, or default working directory, or a claim too old to record them. These
        never block use; decide per warning whether to keep working or dispose and reprovision.
        """

        return await client.info(name)

    @mcp.tool(annotations=_READ_ONLY)
    async def list_sandboxes(
        limit: Annotated[int, Field(ge=1, le=100, description="Maximum managed claims in this page.")],
        continue_token: Annotated[
            str | None,
            Field(default=None, description="Opaque token returned by the previous page; omit for the first page."),
        ] = None,
    ) -> SandboxListPage:
        """List Console's managed sandboxes as one bounded page.

        Use this to discover names and current lifecycle state; use ``get_sandbox_info`` for a
        focused follow-up. Each entry carries the same ``warnings`` as ``get_sandbox_info``, so a
        page can be scanned by warning ``kind`` for sandboxes that have drifted from the current
        configuration. Pass the returned ``continue_token`` unchanged to fetch the next page. Only
        service-owned claims are returned, and raw Kubernetes objects and secrets are omitted.
        """

        return await client.list(limit=limit, continue_token=continue_token)

    @mcp.tool(annotations=_DISPOSE)
    async def dispose_sandbox(name: SandboxName) -> DisposeSandboxResult:
        """Delete a managed SandboxClaim and release its environment.

        Use this when the sandbox is no longer needed or must be recreated after configuration
        changes. The Agent Sandbox controller performs dependent cleanup according to the claim's
        deletion policy. Repeating disposal is safe and reports ``deleted=false`` once absent;
        foreign claims are never deleted.
        """

        return await client.dispose(name)

    return mcp
