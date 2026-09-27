"""Builds the `ApiObjectMetadata` every generated resource needs."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata


def metadata(
    name: str, namespace: str, *, labels: dict[str, str] | None = None, annotations: dict[str, str] | None = None
) -> ApiObjectMetadata:
    return ApiObjectMetadata(name=name, namespace=namespace, labels=labels, annotations=annotations)
