"""Shared helpers for writing generated cdk8s manifests."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from cdk8s import App, Chart, Yaml
from cdk8s_plus_34 import ConfigMap, k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDecryption

from cluster.cdk8s.flux import SOPS_DECRYPTION
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.metadata import metadata

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


def config_map_chart(app: App, *, chart_name: str, configmap_name: str, namespace: str, data: dict[str, str]) -> Chart:
    chart = Chart(app, chart_name, disable_resource_name_hashes=True)
    ConfigMap(chart, "config", metadata=metadata(configmap_name, namespace), data=data)
    return chart


def write_charts(root: Path, app_dir: str, *chart_builders: Callable[[App], Chart]) -> None:
    """Synthesize charts into one Kustomization directory."""
    out_dir = root / app_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    for build in chart_builders:
        build(app)
    app.synth()


def sops_decryption(resources: Sequence[str]) -> KustomizationSpecDecryption | None:
    """Return the Flux decryption block when a directory lists an encrypted Secret."""
    if not any(resource.endswith(".sops.yaml") for resource in resources):
        return None
    return SOPS_DECRYPTION


def write_namespace(
    root: Path, directory: str, *, name: str, labels: Mapping[str, str], annotations: Mapping[str, str] | None = None
) -> None:
    """Write `namespace.k8s.yaml` into `directory`, whose hand-written `kustomization.yaml`
    lists it, so the Namespace stays owned by that directory's Kustomization."""
    out_dir = root / directory
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    k8s.KubeNamespace(
        Chart(app, "namespace", disable_resource_name_hashes=True),
        "namespace",
        metadata=k8s.ObjectMeta(name=name, labels=dict(labels), annotations=dict(annotations) if annotations else None),
    )
    app.synth()
