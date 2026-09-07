"""`hostexec.bash` auto-approval: confined to named hosts, any `run_as`, any `cmd`.

`hostexec`'s execution authority is unconditional and unaffected by this policy: the console still
mints the approving Operator's own short-lived, per-host Authentik token on every call, whether a
human clicked approve or this policy matched (haku/docs/security.md invariant #9). `bash`'s whole
argument set is `host`, `run_as`, `cmd`, `cwd`, `max_bytes`, `timeout_ms` -- flat scalars, none of
which can smuggle a second target the way Home Assistant's free-form `data` blob can -- so the only
thing worth constraining is which host the call reaches at all.
"""

from __future__ import annotations

import logging
from typing import Any

from haku.console.auto_approval.decision import AutoApprovalDecision, AutoApproved, NotAutoApproved

logger = logging.getLogger(__name__)

BASH_TOOL = "bash"


def evaluate_host_scoped(tool_name: str, arguments: dict[str, Any], hosts: set[str]) -> AutoApprovalDecision:
    """Evaluate one `hostexec.bash` call against the configured host allow-list."""
    try:
        if tool_name != BASH_TOOL:
            return NotAutoApproved(f"{tool_name!r} is not the reviewed hostexec tool")

        host = arguments.get("host")
        if not isinstance(host, str) or not host:
            return NotAutoApproved("auto-approval requires a string host")
        if host not in hosts:
            return NotAutoApproved(f"{host} is not a host this policy auto-approves")

        return AutoApproved(f"host {host} is auto-approved regardless of run_as or cmd")
    except Exception:
        logger.exception("auto-approval evaluation failed tool=%s", tool_name)
        return NotAutoApproved("hostexec auto-approval evaluation failed")
