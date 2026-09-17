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

from cdk8s import Testing
from flux_kustomize.io.fluxcd.toolkit.kustomize import Kustomization, KustomizationSpec
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

NAMESPACE = "ducktape-flux"  # shared Flux namespace every generated Kustomization CR lives in


def flux_kustomization(name: str, *, spec: KustomizationSpec) -> dict[str, object]:
    """Return a Flux `Kustomization` custom resource as a plain manifest dict.

    `spec` is the generated typed `KustomizationSpec` (//third_party/flux:kustomization) --
    build it directly rather than through a hand-rolled subset of its fields; this only
    supplies the metadata/chart/synth plumbing that isn't part of the CRD's own spec.
    """
    chart = Testing.chart()
    Kustomization(chart, name, metadata={"name": name, "namespace": NAMESPACE}, spec=spec)
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
