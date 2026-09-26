"""The pod-level hardening kwargs shared by this repo's hardened single-container
Deployments and Jobs: a pinned `pod_metadata` label, no auto-mounted ServiceAccount
token, no injected Docker-links service env vars, and a fixed non-root UID/GID.

Deliberately excluded: `select=False` plus the caller's own
`deployment.select(LabelSelector.of(labels=...))` (Deployment-specific selector
preservation for Flux adoption -- a Job has no such immutable-selector problem) stays
each call site's own responsibility.
"""

from __future__ import annotations

from typing import TypedDict

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import PodSecurityContextProps


class HardenedPodDefaults(TypedDict):
    pod_metadata: ApiObjectMetadata
    automount_service_account_token: bool
    enable_service_links: bool
    security_context: PodSecurityContextProps


def hardened_pod_defaults(labels: dict[str, str], *, uid: int, gid: int) -> HardenedPodDefaults:
    """Spread with `**` into a `Deployment(...)`/`Job(...)` call."""
    return HardenedPodDefaults(
        pod_metadata=ApiObjectMetadata(labels=labels),
        automount_service_account_token=False,
        enable_service_links=False,
        security_context=PodSecurityContextProps(ensure_non_root=True, user=uid, group=gid),
    )
