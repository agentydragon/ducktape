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

import jsii
from cdk8s import ApiObject, ApiObjectMetadata, App, Chart
from constructs import IValidation
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    Kustomization,
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecHealthChecks,
    KustomizationSpecImages,
    KustomizationSpecPatches,
    KustomizationSpecPostBuild,
    KustomizationSpecPostBuildSubstituteFrom,
    KustomizationSpecPostBuildSubstituteFromKind,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

NAMESPACE = "ducktape-flux"  # shared Flux namespace every generated Kustomization CR lives in
SOPS_DECRYPTION = KustomizationSpecDecryption(
    provider=KustomizationSpecDecryptionProvider.SOPS,
    secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
)
# `${LETSENCRYPT_ISSUER}` from cert_manager/issuer_config.py's ConfigMap, which is reflected
# into NAMESPACE: Flux reads substitution sources from the Kustomization's own namespace.
CERT_MANAGER_ISSUER_CONFIG = "cert-manager-issuer-config"
CERT_MANAGER_ISSUER_SUBSTITUTION = KustomizationSpecPostBuild(
    substitute_from=[
        KustomizationSpecPostBuildSubstituteFrom(
            kind=KustomizationSpecPostBuildSubstituteFromKind.CONFIG_MAP, name=CERT_MANAGER_ISSUER_CONFIG
        )
    ]
)


@jsii.implements(IValidation)
class _WaitExcludesHealthChecks:
    """kustomize-controller ignores `spec.healthChecks` when `spec.wait` is true: it
    health-checks every applied object instead, so such a list is dead config. Checked on
    the rendered objects, so a Kustomization built without `flux_kustomization` is covered."""

    def __init__(self, chart: Chart) -> None:
        self._chart = chart

    def validate(self) -> list[str]:
        rendered = (
            cast(ApiObject, node).to_json() for node in self._chart.node.find_all() if ApiObject.is_api_object(node)
        )
        return [
            f"Kustomization/{obj['metadata']['name']}: wait: true ignores healthChecks; drop the list or set wait off"
            for obj in rendered
            if obj["kind"] == "Kustomization"
            and obj["apiVersion"].startswith("kustomize.toolkit.fluxcd.io/")
            and obj["spec"].get("wait")
            and obj["spec"].get("healthChecks")
        ]


def kustomizations_chart(app: App) -> Chart:
    """The shared chart every Flux Kustomization node is built in; synth fails on a
    Kustomization setting both `wait` and `healthChecks`."""
    chart = Chart(app, "kustomizations", disable_resource_name_hashes=True)
    chart.node.add_validation(_WaitExcludesHealthChecks(chart))
    return chart


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
    source: ArtifactGeneratorSpecArtifacts | KustomizationSpecSourceRef,
    *,
    path: str | None = None,
    interval: str = "10m",
    retry_interval: str | None = "1m",
    timeout: str | None = None,
    prune: bool = True,
    wait: bool | None = True,
    suspend: bool | None = None,
    deletion_policy: KustomizationSpecDeletionPolicy | None = None,
    decryption: KustomizationSpecDecryption | None = None,
    depends_on: Sequence[KustomizationSpecDependsOn] | None = None,
    health_checks: Sequence[KustomizationSpecHealthChecks] | None = None,
    health_check_exprs: Sequence[KustomizationSpecHealthCheckExprs] | None = None,
    post_build: KustomizationSpecPostBuild | None = None,
    target_namespace: str | None = None,
    service_account_name: str | None = None,
    images: Sequence[KustomizationSpecImages] | None = None,
    patches: Sequence[KustomizationSpecPatches] | None = None,
    description: str | None = None,
    namespace: str = NAMESPACE,
    annotations: dict[str, str] | None = None,
) -> Kustomization:
    """Add and return a Flux `Kustomization` custom resource in `chart`.

    `source` is the node's `ArtifactGenerator` artifact, from which `sourceRef` and `path`
    (its first directory) derive, or a direct `sourceRef` -- a `GitRepository` -- which
    takes an explicit `path`. The spec keywords are `KustomizationSpec` fields under the
    same names and types. Our policy, which a node overrides only where it differs:
    `interval="10m"`, `retry_interval="1m"`, `prune=True`, `wait=True`. `None` leaves a
    field unset, so Flux's own default applies (which for `retry_interval` is `interval`
    and for `wait` is false). `health_checks` needs `wait` off: with `wait=True` Flux ignores
    them. A `KustomizationSpec` field no node sets yet becomes a keyword here when one first
    needs it.
    `description` becomes the `description` annotation (cluster/AGENTS.md).
    """
    if wait and health_checks:
        raise ValueError(f"{name=}: wait=True health-checks every applied object and Flux ignores health_checks")
    match source:
        case ArtifactGeneratorSpecArtifacts():
            if path is not None:
                raise ValueError(f"{name=}: an artifact source derives its path; got {path=}")
            source_ref = KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=source.name, namespace=NAMESPACE
            )
            path = "./" + source.copy[0].to.removeprefix("@artifact/").removesuffix("/")
        case KustomizationSpecSourceRef():
            if path is None:
                raise ValueError(f"{name=}: a direct sourceRef needs an explicit path")
            source_ref = source

    metadata_annotations = dict(annotations or {})
    if description is not None:
        metadata_annotations["description"] = description

    return Kustomization(
        chart,
        name,
        metadata=ApiObjectMetadata(name=name, namespace=namespace, annotations=metadata_annotations or None),
        spec=KustomizationSpec(
            source_ref=source_ref,
            path=path,
            interval=interval,
            retry_interval=retry_interval,
            timeout=timeout,
            prune=prune,
            wait=wait,
            suspend=suspend,
            deletion_policy=deletion_policy,
            decryption=decryption,
            depends_on=depends_on,
            health_checks=health_checks,
            health_check_exprs=health_check_exprs,
            post_build=post_build,
            target_namespace=target_namespace,
            service_account_name=service_account_name,
            images=images,
            patches=patches,
        ),
    )


def flux_kustomization_depends_on(dependency: Kustomization) -> KustomizationSpecDependsOn:
    """Represent a previously constructed Flux Kustomization as a Flux dependsOn entry."""
    return KustomizationSpecDependsOn(name=dependency.name, namespace=dependency.metadata.namespace)


def flux_kustomization_depends_on_many(*dependencies: Kustomization) -> list[KustomizationSpecDependsOn]:
    """Represent several previously constructed Flux Kustomizations as dependsOn entries."""
    return [flux_kustomization_depends_on(dependency) for dependency in dependencies]


class ConfigMapArgs(BaseModel):
    """One `configMapGenerator` entry: a ConfigMap kustomize renders from hand-written files
    beside the `kustomization.yaml` or from literals, with its content-hash name suffix and
    reference rewriting. A construct mounting it references `name`
    (`ConfigMap.from_config_map_name`)."""

    name: str
    namespace: str
    files: list[str] | None = Field(
        default=None, description="File names relative to the directory; each becomes a key of that name."
    )
    literals: list[str] | None = Field(default=None, description="`KEY=value` entries, split at the first `=`.")


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
