"""Builds the `ApiObjectMetadata` every generated resource needs."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata

RELOADER_ANNOTATION = "reloader.stakater.com/auto"
"""Stakater Reloader's per-workload restart-on-config-change trigger (see `reloader.py`)."""

RELOADER_AUTO: dict[str, str] = {RELOADER_ANNOTATION: "true"}
"""Ready-to-spread annotations enabling Reloader's auto-restart for a workload."""


def metadata(
    name: str, namespace: str, *, labels: dict[str, str] | None = None, annotations: dict[str, str] | None = None
) -> ApiObjectMetadata:
    return ApiObjectMetadata(name=name, namespace=namespace, labels=labels, annotations=annotations)
