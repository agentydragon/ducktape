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
See cluster/docs/cdk8s.md.
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
class FluxKustomizationSpec:
    name: str
    path: str
    interval: str
    # Names of Kustomizations this one depends on. All live in the shared Flux
    # namespace, same as this one -- KustomizationSpecDependsOn's own `namespace`
    # already defaults to "the namespace of the resource object that contains the
    # reference" when omitted, so there's nothing to set explicitly here.
    depends_on: tuple[str, ...] = ()
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
            depends_on=[KustomizationSpecDependsOn(name=name) for name in spec.depends_on] or None,
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
        default=None, description="Paths to Kustomize Component directories, per kustomize.config.k8s.io/v1beta1."
    )


def kustomize_kustomization(
    *, resources: list[str], namespace: str | None = None, components: Sequence[str] = ()
) -> dict[str, object]:
    """Return the plain `kustomize.config.k8s.io` `Kustomization` listing `resources`.

    `components` names ordinary Kustomize `Component` directories. Today's only
    caller passes a hand-written one carrying a Flux image-automation marker (see
    cluster/docs/cdk8s.md) -- that's specific to that use, not a property of this
    field; a generated Component directory would work the same way.
    """
    manifest = _KustomizeKustomization(
        namespace=namespace, resources=resources, components=list(components) if components else None
    )
    return manifest.model_dump(by_alias=True, exclude_none=True)
