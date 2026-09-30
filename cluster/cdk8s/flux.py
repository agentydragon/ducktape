"""Builds the Flux `Kustomization` custom resource each converted directory needs,
plus the (kustomize) `kustomization.yaml` referencing its manifests.

The Flux `Kustomization` CR is built from //cluster/cdk8s/providers/flux:kustomization's
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
from typing import cast

import jsii
from cdk8s import ApiObject, ApiObjectMetadata, App, Chart
from constructs import IValidation
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
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
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.providers.flux.flux_kustomization import Kustomization

NAMESPACE = "ducktape-flux"  # shared Flux namespace every generated Kustomization CR lives in
SOPS_DECRYPTION = KustomizationSpecDecryption(
    provider=KustomizationSpecDecryptionProvider.SOPS,
    secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
)


@dataclass(frozen=True)
class RenderedDirectory:
    """A directory `generation.write_directory` wrote, as its Flux Kustomization reads it:
    `sourceRef` and `path` come from the artifact packaging it, and `decryption` is set when a
    hand-written sibling is SOPS ciphertext."""

    artifact: ArtifactGeneratorSpecArtifacts
    decryption: KustomizationSpecDecryption | None


def artifact_directories(artifact: ArtifactGeneratorSpecArtifacts) -> list[str]:
    """The repo-relative directories `artifact` copies, in copy order. The first is its consumer's
    Kustomization directory; later ones are shared bases and Components that Kustomization
    references.

    Raises on any shape `artifact_generators.artifact` does not build -- no copies, or a copy
    that is not one whole directory copied to the same path -- since the Kustomization's `path`
    would otherwise silently point at the wrong directory.
    """
    directories = []
    for copy in artifact.copy:
        directory = copy.to.removeprefix("@artifact/").removesuffix("/")
        if (
            not directory
            or copy.to != f"@artifact/{directory}/"
            or copy.from_ != f"@repo/{directory}/**"
            or copy.exclude is not None
            or copy.strategy is not None
        ):
            raise ValueError(f"{artifact.name=}: not a whole-directory copy: {copy.from_=} {copy.to=}")
        directories.append(directory)
    if not directories:
        raise ValueError(f"{artifact.name=} copies nothing")
    return directories


def artifact_directory(artifact: ArtifactGeneratorSpecArtifacts) -> str:
    """The directory `artifact` copies first: its consumer's Kustomization directory."""
    return artifact_directories(artifact)[0]


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
    source: RenderedDirectory | ArtifactGeneratorSpecArtifacts | KustomizationSpecSourceRef,
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

    `source` is the node's `RenderedDirectory`, from which `sourceRef`, `path` and
    `decryption` derive; or its `ArtifactGenerator` artifact, from which `sourceRef` and
    `path` (its first directory) derive; or a direct `sourceRef` -- a `GitRepository` --
    which takes an explicit `path`. The spec keywords are `KustomizationSpec` fields under
    the same names and types. Our policy, which a node overrides only where it differs:
    `interval="10m"`, `retry_interval="1m"`, `prune=True`, `wait=True`. `None` leaves a
    field unset, so Flux's own default applies (which for `retry_interval` is `interval`
    and for `wait` is false). `health_checks` needs `wait` off: with `wait=True` Flux ignores
    them. A `KustomizationSpec` field no node sets yet becomes a keyword here when one first
    needs it.
    `description` becomes the `description` annotation (cluster/AGENTS.md).
    """
    if wait and health_checks:
        raise ValueError(f"{name=}: wait=True health-checks every applied object and Flux ignores health_checks")
    if isinstance(source, RenderedDirectory):
        if decryption is not None:
            raise ValueError(f"{name=}: a rendered directory derives its decryption; got {decryption=}")
        decryption = source.decryption
        source = source.artifact
    match source:
        case ArtifactGeneratorSpecArtifacts():
            if path is not None:
                raise ValueError(f"{name=}: an artifact source derives its path; got {path=}")
            source_ref = KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=source.name, namespace=NAMESPACE
            )
            path = f"./{artifact_directory(source)}"
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
    )


def flux_kustomization_depends_on(dependency: Kustomization) -> KustomizationSpecDependsOn:
    """Represent a previously constructed Flux Kustomization as a Flux dependsOn entry."""
    return KustomizationSpecDependsOn(name=dependency.name, namespace=dependency.metadata.namespace)


def flux_kustomization_depends_on_many(*dependencies: Kustomization) -> list[KustomizationSpecDependsOn]:
    """Represent several previously constructed Flux Kustomizations as dependsOn entries."""
    return [flux_kustomization_depends_on(dependency) for dependency in dependencies]


class GeneratorOptions(BaseModel):
    """A generator entry's `options`, or the Kustomization's `generatorOptions` covering every
    entry, per kustomize.config.k8s.io/v1beta1."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    annotations: dict[str, str] | None = None
    disable_name_suffix_hash: bool | None = None


class ConfigMapArgs(BaseModel):
    """One `configMapGenerator` entry: a ConfigMap kustomize renders from hand-written files
    beside the `kustomization.yaml` or from literals, with its content-hash name suffix and
    reference rewriting. A construct mounting it references `name`
    (`ConfigMap.from_config_map_name`)."""

    name: str
    namespace: str | None = Field(
        description="None only in a base whose overlays set the namespace, as on the objects that mount it: "
        "kustomize rewrites a reference to the hashed name only within one namespace."
    )
    options: GeneratorOptions | None = None
    files: list[str] | None = Field(
        default=None, description="File names relative to the directory; each becomes a key of that name."
    )
    literals: list[str] | None = Field(default=None, description="`KEY=value` entries, split at the first `=`.")


class PatchFile(BaseModel):
    """A `patches` entry naming a strategic-merge patch file beside the `kustomization.yaml`;
    each object in it names the object it patches."""

    path: str


class PatchTarget(BaseModel):
    """A `patches` entry's `target` (kustomize's `Selector`)."""

    kind: str
    name: str
    namespace: str


class Json6902Patch(BaseModel):
    """A `patches` entry holding a JSON6902 patch (RFC 6902 operations, as YAML) of `target`."""

    patch: str
    target: PatchTarget


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
    generator_options: GeneratorOptions | None = None
    config_map_generator: list[ConfigMapArgs] | None = None
    configurations: list[str] | None = Field(
        default=None, description="Transformer configuration files, relative to the directory."
    )
    patches: list[PatchFile | Json6902Patch] | None = None


def kustomize_kustomization(
    *,
    resources: list[str],
    namespace: str | None = None,
    components: Sequence[str] = (),
    generator_options: GeneratorOptions | None = None,
    config_map_generator: Sequence[ConfigMapArgs] = (),
    configurations: Sequence[str] = (),
    patches: Sequence[PatchFile | Json6902Patch] = (),
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
        generator_options=generator_options,
        config_map_generator=list(config_map_generator) if config_map_generator else None,
        configurations=list(configurations) if configurations else None,
        patches=list(patches) if patches else None,
    )
    return manifest.model_dump(by_alias=True, exclude_none=True)
