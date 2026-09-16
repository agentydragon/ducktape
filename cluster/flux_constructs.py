"""Builds the Flux `Kustomization` custom resource each converted directory needs,
plus the (kustomize) `kustomization.yaml` referencing its manifests.

The Flux `Kustomization` CR is built from //third_party/flux:kustomization's
generated cdk8s constructs (see devinfra/js/cdk8s_import.bzl) rather than a plain
dict, so a malformed dependsOn entry or sourceRef kind fails at synth time instead
of silently emitting invalid YAML. The plain (non-CRD) kustomize.config.k8s.io
Kustomization has a real upstream JSON Schema (SchemaStore's kustomization.json),
but `cdk8s import` only ingests Kubernetes CustomResourceDefinition-shaped input --
tested directly against it, it fails trying to parse the schema itself as a CRD.
Converting that schema by hand into a CRD envelope is real, undertaken work, not a
`cdk8s import <url>` away, so this stays a hand-written Pydantic model instead: no
generated schema validation, but at least real field types instead of a bare dict.
See cluster/docs/plans/cdk8s_adoption.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from cdk8s import Testing
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    Kustomization,
    KustomizationSpec,
    KustomizationSpecDependsOn,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

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
    chart = Testing.chart()
    Kustomization(
        chart,
        spec.name,
        metadata={"name": spec.name, "namespace": _NAMESPACE},
        spec=KustomizationSpec(
            interval=spec.interval,
            path=spec.path,
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT,
                name=spec.source_name or spec.name,
                namespace=_NAMESPACE,
            ),
            timeout=spec.timeout,
            depends_on=[KustomizationSpecDependsOn(name=dep.name, namespace=dep.namespace) for dep in spec.depends_on]
            or None,
        ),
    )
    (manifest,) = Testing.synth(chart)
    assert isinstance(manifest, dict)
    return manifest


class _KustomizeKustomization(BaseModel):
    """The plain (non-CRD) `kustomize.config.k8s.io/v1beta1` `Kustomization`."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    api_version: str = "kustomize.config.k8s.io/v1beta1"
    kind: str = "Kustomization"
    namespace: str | None = None
    resources: list[str]
    components: list[str] | None = Field(
        default=None, description="Directories the generator never writes, e.g. a hand-written image-pins/ Component."
    )


def kustomize_kustomization(
    *, resources: list[str], namespace: str | None = None, components: Sequence[str] = ()
) -> dict[str, object]:
    """Return the plain `kustomize.config.k8s.io` `Kustomization` listing `resources`.

    `components` names directories the generator never writes -- e.g. a hand-written
    Kustomize `Component` carrying a Flux image-automation marker (see
    cluster/docs/plans/cdk8s_adoption.md).
    """
    manifest = _KustomizeKustomization(
        namespace=namespace, resources=resources, components=list(components) if components else None
    )
    return manifest.model_dump(by_alias=True, exclude_none=True)
