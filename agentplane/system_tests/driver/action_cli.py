"""Arguments shared by system scenarios that exercise the Sandbox Action CLI."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

# The environment disk mounts the CLI at this path (agentplane/env/images/BUILD.bazel).
CLI = "/run/agentplane/environment/bin/agentplane"


def submission(group: str, action: str, arguments: dict[str, Any], *, title: str, wait: bool) -> list[str]:
    """The `agentplane` arguments that submit one Action request under a fresh key."""
    return [
        "actions",
        "submit",
        f"{group}/{action}",
        "--title",
        title,
        "--arguments",
        json.dumps(arguments),
        "--key",
        str(uuid4()),
        *(["--wait"] if wait else []),
    ]
