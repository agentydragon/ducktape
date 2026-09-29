"""Shared helpers for writing generated cdk8s manifests."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from string import Template

from cdk8s import ApiObjectMetadata, App, Chart, Names, Yaml
from cdk8s_plus_34 import ConfigMap
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDecryption
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import namespaces
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    ConfigMapArgs,
    RenderedDirectory,
    artifact_directory,
    kustomize_kustomization,
)
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import AgentReadable, Vpa
from util.bazel.runfiles import get_required_path, own_repo_rlocation

CNPG_DATABASE_READY = (
    "has(status.applied) && status.applied && "
    "has(status.observedGeneration) && status.observedGeneration == metadata.generation"
)


_GENERATED_README = """\
# Generated Flux manifests

Every file in this tree is written by the cdk8s generators in `cluster/cdk8s`; do not edit it.
Change the generator and regenerate with `bb run //cluster/cdk8s:generate_manifests`.
`//cluster/cdk8s:test_generate_manifests` fails on any file here the generator does not
write. Layout: `cluster/docs/cdk8s.md`.
"""


def write_generated_readme(root: Path) -> None:
    out_dir = root / GENERATED_ROOT
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "README.md").write_text(_GENERATED_README)


def write_yaml(path: Path, manifest: dict[str, object]) -> None:
    path.write_text(Yaml.format_objects([manifest]))


class _SourceTemplate(Template):
    # Not `$`, which shell and nginx payloads spend on their own variables.
    delimiter = "@"


def render_source_file(root: Path, directory: str, source: str, /, **values: object) -> str:
    """Render `source`, a repo-relative native file shipped as this generator's runfiles data,
    into `directory` under its own name; return that name for the `kustomization.yaml`.
    Each `@{name}` placeholder takes the keyword `name`, a value a generator module owns."""
    name = PurePosixPath(source).name
    template = _SourceTemplate(get_required_path(own_repo_rlocation(source)).read_text())
    if unused := values.keys() - set(template.get_identifiers()):
        raise ValueError(f"{source=} has no placeholder for {unused=}")
    out_dir = root / directory
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / name).write_text(template.substitute(values))
    return name


def config_map_chart(app: App, *, chart_name: str, configmap_name: str, namespace: str, data: dict[str, str]) -> Chart:
    chart = Chart(app, chart_name, disable_resource_name_hashes=True)
    ConfigMap(chart, "config", metadata=ApiObjectMetadata(name=configmap_name, namespace=namespace), data=data)
    return chart


def write_charts(root: Path, app_dir: str, *chart_builders: Callable[[App], Chart]) -> list[str]:
    """Synthesize charts into one Kustomization directory; return the files written, in chart order."""
    out_dir = root / app_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    for build in chart_builders:
        build(app)
    app.synth()
    # `App.synth`'s file name for a chart while no chart depends on another (cdk8s `SimpleChartNamer`).
    return [Names.to_dns_label(chart) + app.output_file_extension for chart in app.charts]


def write_directory(
    root: Path,
    artifact: ArtifactGeneratorSpecArtifacts,
    *chart_builders: Callable[[App], Chart],
    siblings: Sequence[str] = (),
    namespace: str | None = None,
    components: Sequence[str] = (),
    config_map_generator: Sequence[ConfigMapArgs] = (),
) -> RenderedDirectory:
    """Synthesize a component's charts into the directory `artifact` packages, and write its
    `kustomization.yaml` listing them, then `siblings`: the hand-written files beside them.
    `namespace`, `components` and `config_map_generator` are `kustomize_kustomization`'s.

    For a directory whose `kustomization.yaml` the generator owns: under `GENERATED_ROOT`, or
    under `HAND_WRITTEN_ROOT` beside the hand-written files it names (a `.sops.yaml` sibling,
    an `image-pins` Component, a generated ConfigMap's source file).
    One keeping a hand-written `kustomization.yaml` uses `write_charts`. The returned
    directory's `decryption` is set exactly when a sibling is SOPS ciphertext.
    """
    directory = artifact_directory(artifact)
    if len(artifact.copy) != 1:
        raise ValueError(f"{artifact.name=}: the writer lists one directory; this artifact also copies shared bases")
    resources = [*write_charts(root, directory, *chart_builders), *siblings]
    write_yaml(
        root / directory / "kustomization.yaml",
        kustomize_kustomization(
            resources=resources, namespace=namespace, components=components, config_map_generator=config_map_generator
        ),
    )
    return RenderedDirectory(artifact=artifact, decryption=sops_decryption(siblings))


def sops_decryption(resources: Sequence[str]) -> KustomizationSpecDecryption | None:
    """Return the Flux decryption block when a directory lists an encrypted Secret."""
    if not any(resource.endswith(".sops.yaml") for resource in resources):
        return None
    return SOPS_DECRYPTION


def write_namespace(
    root: Path,
    directory: str,
    *,
    name: str,
    vpa: Vpa,
    agent_readable: AgentReadable | None,
    labels: Mapping[str, str] | None = None,
    annotations: Mapping[str, str] | None = None,
) -> None:
    """Write only `namespace.k8s.yaml`, `namespaces.namespace`'s Namespace, into `directory`; its
    `kustomization.yaml`, hand-written or generated elsewhere, lists it, so the Namespace stays
    owned by that directory's Kustomization."""
    out_dir = root / directory
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    namespaces.namespace(
        Chart(app, "namespace", disable_resource_name_hashes=True),
        "namespace",
        name=name,
        vpa=vpa,
        agent_readable=agent_readable,
        labels=labels,
        annotations=annotations,
    )
    app.synth()
