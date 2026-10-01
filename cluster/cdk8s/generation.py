"""Shared helpers for writing generated cdk8s manifests."""

from __future__ import annotations

import posixpath
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath

from cdk8s import ApiObjectMetadata, App, Chart, Yaml
from cdk8s_plus_34 import ConfigMap
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDecryption
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import namespaces
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    ConfigMapArgs,
    GeneratorOptions,
    Json6902Patch,
    PatchFile,
    RenderedDirectory,
    artifact_directories,
    kustomize_kustomization,
)
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import AgentReadable, Vpa
from util.bazel.runfiles import get_required_path, own_repo_rlocation

CNPG_DATABASE_READY = (
    "has(status.applied) && status.applied && "
    "has(status.observedGeneration) && status.observedGeneration == metadata.generation"
)
_PATCH_FILE = "patches.k8s.yaml"


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


def copy_source_file(root: Path, directory: str, source: str) -> str:
    """Copy `source`, a repo-relative native file shipped as this generator's runfiles data,
    into `directory` under its own name; return that name for the `kustomization.yaml`."""
    name = PurePosixPath(source).name
    out_dir = root / directory
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / name).write_bytes(get_required_path(own_repo_rlocation(source)).read_bytes())
    return name


def config_map_chart(app: App, *, chart_name: str, configmap_name: str, namespace: str, data: dict[str, str]) -> Chart:
    chart = Chart(app, chart_name, disable_resource_name_hashes=True)
    ConfigMap(chart, "config", metadata=ApiObjectMetadata(name=configmap_name, namespace=namespace), data=data)
    return chart


def manifest_file(directory: str) -> str:
    """The one generated resource file of `directory`, named after the directory itself."""
    return f"{PurePosixPath(directory).name}.k8s.yaml"


def _write_app(path: Path, app: App) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # `App.synth` would write one file per chart; `synth_yaml` runs the same validation.
    path.write_text(app.synth_yaml())


def _app(chart_builders: Sequence[Callable[[App], Chart]]) -> App:
    app = App()
    for build in chart_builders:
        build(app)
    return app


def write_app(root: Path, directory: str, app: App, *, manifest_name: str | None = None) -> str:
    """Write every chart of `app`, in chart order, into one generated file in `directory`.

    The file defaults to `manifest_file(directory)`. A flat Kustomize root with several
    independently synthesized chart apps may pass a distinct `manifest_name` for each.
    """
    name = manifest_name or manifest_file(directory)
    _write_app(root / directory / name, app)
    return name


def write_charts(
    root: Path, directory: str, *chart_builders: Callable[[App], Chart], manifest_name: str | None = None
) -> str:
    """Synthesize charts, in order, into one generated file in `directory`; return its name."""
    return write_app(root, directory, _app(chart_builders), manifest_name=manifest_name)


def write_directory(
    root: Path,
    artifact: ArtifactGeneratorSpecArtifacts,
    *chart_builders: Callable[[App], Chart],
    siblings: Sequence[str] = (),
    remote_resources: Sequence[str] = (),
    namespace: str | None = None,
    components: Sequence[str] = (),
    generator_options: GeneratorOptions | None = None,
    config_map_generator: Sequence[ConfigMapArgs] = (),
    configurations: Sequence[str] = (),
    patch_charts: Sequence[Callable[[App], Chart]] = (),
    json6902_patches: Sequence[Json6902Patch] = (),
) -> RenderedDirectory:
    """Synthesize a component's charts into the one generated file of the directory `artifact`
    packages (`write_charts`), and write its `kustomization.yaml` listing it, then `siblings`:
    the directory's other files, then `remote_resources`: URLs kustomize fetches at build time,
    such as an upstream release manifest. `patch_charts` are synthesized into
    `patches.k8s.yaml` beside it and listed as strategic-merge patches, before
    `json6902_patches`. `namespace`, `components`, `generator_options`, `config_map_generator`
    and `configurations` are `kustomize_kustomization`'s.

    For a directory whose `kustomization.yaml` the generator owns: under `GENERATED_ROOT`, or
    under `HAND_WRITTEN_ROOT` beside the hand-written files it names (a `.sops.yaml` sibling,
    an `image-pins` Component, a generated ConfigMap's source file).
    One keeping a hand-written `kustomization.yaml` uses `write_charts`. The returned
    directory's `decryption` is set exactly when a sibling is SOPS ciphertext. Every copy of
    `artifact` after the first must be a Component `components` names: the `image-pins`
    directory a `GENERATED_ROOT` directory includes across the roots
    (cluster/docs/cdk8s_remainder.md § Mixed-directory layout).
    """
    directory, *copied = artifact_directories(artifact)
    included = {posixpath.normpath(posixpath.join(directory, component)) for component in components}
    if shared := [base for base in copied if base not in included]:
        raise ValueError(f"{artifact.name=}: the writer lists one directory; this artifact also copies {shared=}")
    out_dir = root / directory
    out_dir.mkdir(parents=True, exist_ok=True)
    resources = [write_charts(root, directory, *chart_builders)] if chart_builders else []
    patch_files: list[PatchFile] = []
    if patch_charts:
        _write_app(out_dir / _PATCH_FILE, _app(patch_charts))
        patch_files.append(PatchFile(path=_PATCH_FILE))
    write_yaml(
        out_dir / "kustomization.yaml",
        kustomize_kustomization(
            resources=[*resources, *siblings, *remote_resources],
            namespace=namespace,
            components=components,
            generator_options=generator_options,
            config_map_generator=config_map_generator,
            configurations=configurations,
            patches=[*patch_files, *json6902_patches],
        ),
    )
    return RenderedDirectory(artifact=artifact, decryption=sops_decryption(siblings))


def sops_decryption(resources: Sequence[str]) -> KustomizationSpecDecryption | None:
    """Return the Flux decryption block when a directory lists an encrypted Secret."""
    if not any(resource.endswith(".sops.yaml") for resource in resources):
        return None
    return SOPS_DECRYPTION


def namespace_chart(
    app: App,
    *,
    name: str,
    vpa: Vpa,
    agent_readable: AgentReadable | None,
    labels: Mapping[str, str] | None = None,
    annotations: Mapping[str, str] | None = None,
) -> Chart:
    """A chart holding only `namespaces.namespace`'s Namespace, for the writer of the directory
    whose Kustomization owns it."""
    chart = Chart(app, "namespace", disable_resource_name_hashes=True)
    namespaces.namespace(
        chart, "namespace", name=name, vpa=vpa, agent_readable=agent_readable, labels=labels, annotations=annotations
    )
    return chart
