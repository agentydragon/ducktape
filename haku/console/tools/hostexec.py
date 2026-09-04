"""haku-console's in-process `hostexec` MCP server.

Runs a shell command on an operator machine (`wyrm2`, `rugged`, `atlas`, …) behind haku-console's
approval queue. Every call is operator-approved by construction — `bash` is never in
`UNCONDITIONAL_AUTO_APPROVE` — and executes under the operator's **own Authentik authority**: on
approval the console mints a short-lived, single-use per-host token, queues it for the configured
outbound node daemon, and `hostexecd` verifies it before dropping
privileges to `run_as`. The daemon's standing routing bearer cannot authorize a command.

Built as a real `FastMCP` server attached via an in-memory transport (the gmail/google_calendar
pattern), so the application service's approval/audit lifecycle runs unchanged. Registered as MCP
server id `hostexec` with an `in_process` backend and `operator_login_identity` credential in
`cluster/k8s/haku/console/config.yaml`. See `haku/docs/security.md` and `haku/hostexec/README.md`.
"""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from haku.console.tools.hostexec_client import HostexecClient
from haku.hostexec.wire import RunAsUser
from mcp_infra.exec.models import BaseExecResult, TimeoutMs

# Wire-frozen id: named by cluster/k8s/haku/console/config.yaml and persisted in
# ledger `server_id` rows — renaming is a config + data migration.
HOSTEXEC_SERVER_ID = "hostexec"

# hostexec-specific: NOT `mcp_infra.exec.models.CMD_DESCRIPTION` (that constant documents the
# argv/execve contract shared by the direct-subprocess and bwrap sandbox backends, which is the
# opposite of hostexec's shell semantics).
BASH_SCRIPT_DESCRIPTION = (
    "Bash script text, run as `bash -c cmd` on the target host. Full shell semantics apply: "
    "pipes, redirects (|, >, <), globs (*), &&/;, quoting, and $VAR expansion are all "
    "interpreted exactly as in an interactive bash shell. This is NOT an argv vector — pass a "
    'single script string like "rg -n foo src/ | head -20", not a list of tokens.'
)


def build_mcp(client: HostexecClient) -> FastMCP:
    mcp: FastMCP = FastMCP(
        name=HOSTEXEC_SERVER_ID,
        instructions="Run a shell command on an operator machine (e.g. wyrm2, rugged, atlas). Every call is gated by "
        "the operator-approval queue — there is no autonomous path — and runs as the requested POSIX user under "
        "the operator's own Authentik authority.",
    )

    @mcp.tool
    async def bash(
        host: Annotated[
            str, Field(description="Target host to run on (e.g. 'wyrm2', 'rugged', 'atlas'). Must be in scope.")
        ],
        run_as: Annotated[
            RunAsUser,
            Field(
                description="POSIX username to run as (e.g. 'agentydragon', 'root'). Authorized by the operator's "
                "`hostexec-<user>-<host>` Authentik group."
            ),
        ],
        cmd: Annotated[str, Field(min_length=1, description=BASH_SCRIPT_DESCRIPTION)],
        max_bytes: Annotated[
            int,
            Field(ge=0, le=100_000, description="Max bytes to capture from stdout and stderr. 0 means both are empty."),
        ],
        timeout_ms: TimeoutMs,
        cwd: Annotated[
            str | None, Field(description="Working directory for the command; omit to use hostexecd's default.")
        ] = None,
    ) -> BaseExecResult:
        """Run `cmd` as a bash script (`bash -c cmd`) on `host` as `run_as`. Approval-gated; never auto-approved."""
        return await client.run(host=host, run_as=run_as, cmd=cmd, cwd=cwd, max_bytes=max_bytes, timeout_ms=timeout_ms)

    return mcp
