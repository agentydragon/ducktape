"""Builds the Flux `Kustomization` custom resource each converted directory needs,
plus the (kustomize) `kustomization.yaml` referencing its manifests.

The Flux `Kustomization` CR is built from //cluster/cdk8s/crd_bindings/flux:kustomization's
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
from typing import cast

from cdk8s import ApiObject, ApiObjectMetadata, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    Kustomization,
    KustomizationSpec,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthChecks,
)
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

NAMESPACE = "ducktape-flux"  # shared Flux namespace every generated Kustomization CR lives in


def health_checks(chart: Chart, kinds: Sequence[str]) -> list[KustomizationSpecHealthChecks]:
    """One `healthChecks` entry per object of the listed kinds in `chart`, ordered by `kinds`:
    each entry is the object's own apiVersion/kind/name/namespace, never retyped."""
    # `Chart.api_objects` is direct children only; objects usually sit inside a Construct.
    api_objects = [cast(ApiObject, node) for node in chart.node.find_all() if ApiObject.is_api_object(node)]
    return [
        KustomizationSpecHealthChecks(
            api_version=obj.api_version, kind=obj.kind, name=obj.name, namespace=obj.metadata.namespace
        )
        for obj in sorted((obj for obj in api_objects if obj.kind in kinds), key=lambda obj: kinds.index(obj.kind))
    ]


def flux_kustomization(
    chart: Chart,
    name: str,
    *,
    spec: KustomizationSpec,
    description: str | None = None,
    namespace: str = NAMESPACE,
    annotations: dict[str, str] | None = None,
) -> Kustomization:
    """Add and return a Flux `Kustomization` custom resource in `chart`.

    `spec` is the generated typed `KustomizationSpec` (//cluster/cdk8s/crd_bindings/flux:kustomization) --
    build it directly rather than through a hand-rolled subset of its fields; this only
    supplies metadata that isn't part of the CRD's own spec.
    `description` becomes the `description` annotation (cluster/AGENTS.md).
    """
    metadata_annotations = dict(annotations or {})
    if description is not None:
        metadata_annotations["description"] = description

    return Kustomization(
        chart,
        name,
        metadata=ApiObjectMetadata(name=name, namespace=namespace, annotations=metadata_annotations or None),
        spec=spec,
    )


def flux_kustomization_depends_on(dependency: Kustomization) -> KustomizationSpecDependsOn:
    """Represent a previously constructed Flux Kustomization as a Flux dependsOn entry."""
    return KustomizationSpecDependsOn(name=dependency.name, namespace=dependency.metadata.namespace)


class ConfigMapArgs(BaseModel):
    """One `configMapGenerator` entry: a ConfigMap kustomize renders from hand-written files
    beside the `kustomization.yaml`, with its content-hash name suffix and reference rewriting.
    A construct mounting it references `name` (`ConfigMap.from_config_map_name`)."""

    name: str
    namespace: str
    files: list[str] = Field(description="File names relative to the directory; each becomes a key of that name.")


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
    config_map_generator: list[ConfigMapArgs] | None = None


def kustomize_kustomization(
    *,
    resources: list[str],
    namespace: str | None = None,
    components: Sequence[str] = (),
    config_map_generator: Sequence[ConfigMapArgs] = (),
) -> dict[str, object]:
    """Return the plain `kustomize.config.k8s.io` `Kustomization` listing `resources`.

    `components` names ordinary Kustomize `Component` directories. Today's callers pass
    a hand-written one carrying a Flux image-automation marker (see
    cluster/docs/cdk8s.md) -- that's specific to that use, not a property of this
    field; a generated Component directory would work the same way.
    """
    manifest = _KustomizeKustomization(
        namespace=namespace,
        resources=resources,
        components=list(components) if components else None,
        config_map_generator=list(config_map_generator) if config_map_generator else None,
    )
    return manifest.model_dump(by_alias=True, exclude_none=True)
