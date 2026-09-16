"""Builds the Flux `Kustomization` custom resource each converted directory needs,
plus the (kustomize) `kustomization.yaml` referencing its manifests.

Only models the fields cluster/k8s/litellm/{app,servicemonitor} actually use --
extend as more directories convert rather than pre-guessing the rest of the
`kustomize.toolkit.fluxcd.io` CRD. See cluster/docs/plans/cdk8s_adoption.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

_NAMESPACE = "ducktape-flux"


@dataclass(frozen=True)
class FluxDependency:
    """One `dependsOn` entry. `namespace` defaults to the shared Flux namespace."""

    name: str
    namespace: str = _NAMESPACE


@dataclass(frozen=True)
class FluxKustomizationSpec:
    name: str
    path: str
    interval: str
    depends_on: tuple[FluxDependency, ...] = ()
    timeout: str | None = None
    source_name: str | None = None  # defaults to `name`


def flux_kustomization(spec: FluxKustomizationSpec) -> dict[str, object]:
    """Return the Flux `Kustomization` custom resource as a plain manifest dict."""
    inner: dict[str, object] = {
        "interval": spec.interval,
        "path": spec.path,
        "prune": True,
        "sourceRef": {"kind": "ExternalArtifact", "name": spec.source_name or spec.name, "namespace": _NAMESPACE},
    }
    if spec.timeout is not None:
        inner["timeout"] = spec.timeout
    if spec.depends_on:
        inner["dependsOn"] = [{"name": dep.name, "namespace": dep.namespace} for dep in spec.depends_on]
    return {
        "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
        "kind": "Kustomization",
        "metadata": {"name": spec.name, "namespace": _NAMESPACE},
        "spec": inner,
    }


def kustomize_kustomization(
    *, resources: list[str], namespace: str | None = None, components: Sequence[str] = ()
) -> dict[str, object]:
    """Return the plain `kustomize.config.k8s.io` `Kustomization` listing `resources`.

    `components` names directories the generator never writes -- e.g. a hand-written
    Kustomize `Component` carrying a Flux image-automation marker (see
    cluster/docs/plans/cdk8s_adoption.md).
    """
    manifest: dict[str, object] = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization"}
    if namespace is not None:
        manifest["namespace"] = namespace
    manifest["resources"] = resources
    if components:
        manifest["components"] = list(components)
    return manifest
