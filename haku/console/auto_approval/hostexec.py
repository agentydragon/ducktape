"""`hostexec.bash` auto-approval: confined to named hosts and named `run_as` users, any `cmd`.

`hostexec`'s execution authority is unconditional and unaffected by this policy: the console still
mints the approving Operator's own short-lived, per-host Authentik token on every call, whether a
human clicked approve or this policy matched (haku/docs/security.md invariant #9). `bash`'s whole
argument set is `host`, `run_as`, `cmd`, `cwd`, `max_bytes`, `timeout_ms` -- flat scalars, none of
which can smuggle a second target the way Home Assistant's free-form `data` blob can. `cmd` is left
unconstrained (arbitrary shell is inherent to this tool; confinement is the egress fence, not
argument review), but `run_as` is checked alongside `host`: `hostexecd` runs as root specifically so
it can drop to whichever `run_as` a call names, so an auto-approved `run_as=root` call would let the
Agent read that host's root-owned, mode-0600 daemon-token file (and everything else) with no human
ever seeing the command -- handing back exactly the standing root access invariant #9 exists to
prevent. Restricting to a named unprivileged account keeps the auto-approved surface to whatever
that account can reach.
"""

from __future__ import annotations

import logging
from typing import Any

from haku.console.auto_approval.decision import AutoApprovalDecision, AutoApproved, NotAutoApproved

logger = logging.getLogger(__name__)

BASH_TOOL = "bash"


def evaluate_host_scoped(
    tool_name: str, arguments: dict[str, Any], hosts: set[str], run_as: set[str]
) -> AutoApprovalDecision:
    """Evaluate one `hostexec.bash` call against the configured host/run_as allow-lists."""
    try:
        if tool_name != BASH_TOOL:
            return NotAutoApproved(f"{tool_name!r} is not the reviewed hostexec tool")

        host = arguments.get("host")
        if not isinstance(host, str) or not host:
            return NotAutoApproved("auto-approval requires a string host")
        if host not in hosts:
            return NotAutoApproved(f"{host} is not a host this policy auto-approves")

        call_run_as = arguments.get("run_as")
        if not isinstance(call_run_as, str) or not call_run_as:
            return NotAutoApproved("auto-approval requires a string run_as")
        if call_run_as not in run_as:
            return NotAutoApproved(f"run_as={call_run_as} is not a user this policy auto-approves on {host}")

        return AutoApproved(f"host {host} run_as {call_run_as} is auto-approved regardless of cmd")
    except Exception:
        logger.exception("auto-approval evaluation failed tool=%s", tool_name)
        return NotAutoApproved("hostexec auto-approval evaluation failed")
