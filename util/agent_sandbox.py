"""Helpers for reading Agent Sandbox objects as the upstream controller publishes them.

A reader of these objects would otherwise re-derive the status-condition shape and the annotation
linking a Sandbox to its Pod. That is how the sandbox Actions once searched for a Pod by a label the
controller does not write instead of reading the annotation that actually links the two objects.

What belongs here is only what upstream owns. Each consumer's managed-by labels, naming scheme and
notion of a usable box differ deliberately and stay with it. `condition` serves a caller that reads
the API server's dicts directly; consumers that parse these objects into their own typed models use
the fields they need.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, cast


class OperatingMode(StrEnum):
    """Upstream Sandbox CR operating modes."""

    RUNNING = "Running"
    SUSPENDED = "Suspended"


CLAIMS_PLURAL = "sandboxclaims"

# Where the controller publishes the name of the Pod backing a Sandbox. The Pod carries no label
# tying it back, so this annotation is the only link between the two objects.
POD_NAME_ANNOTATION = "agents.x-k8s.io/pod-name"


def condition(resource: dict[str, Any], condition_type: str) -> dict[str, Any] | None:
    """One `status.conditions` entry as the controller wrote it, or None before it publishes one.

    Returned verbatim so a caller can pass the controller's own `reason` and `message` through:
    `WarmPoolNotFound` tells its reader what to do, where a re-worded summary does not.
    """
    conditions = cast(list[dict[str, Any]], resource.get("status", {}).get("conditions", None) or [])
    for entry in conditions:
        if entry.get("type") == condition_type:
            return entry
    return None


def pod_name(sandbox: dict[str, Any]) -> str:
    """The Pod the controller made for this Sandbox.

    It names the Pod after the Sandbox when it publishes no annotation. Whether that Pod is still
    there is the caller's to confirm — a Sandbox that has been Ready keeps naming a Pod an
    eviction has since taken away.
    """
    metadata = sandbox["metadata"]
    return str((metadata.get("annotations") or {}).get(POD_NAME_ANNOTATION) or metadata["name"])
