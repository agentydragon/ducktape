"""Cluster-level aggregation: ParsedCluster model and parse_cluster orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx

from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT, manifest_files
from cluster.validation.flux import (
    EXTERNAL_ARTIFACT_KIND,
    FLUX_SOURCE_KINDS,
    FluxKustomizationSpec,
    parse_flux_kustomizations,
)
from cluster.validation.k8s import ArtifactGeneratorResource, K8sResource, load_k8s_resources
from cluster.validation.kustomize import (
    KUSTOMIZATION_FILE_NAMES,
    KustomizeFile,
    flux_generated_kustomization,
    has_kustomization_file,
    parse_kustomize_file,
)


@dataclass
class ParsedCluster:
    """All parsed data from the cluster directory - parsed once, used everywhere."""

    kustomize_files: dict[Path, KustomizeFile] = field(default_factory=dict)
    flux_kustomizations: dict[str, FluxKustomizationSpec] = field(default_factory=dict)
    all_yaml_files: set[Path] = field(default_factory=set)
    artifact_copies: dict[tuple[str, str], list[tuple[str, str]]] = field(default_factory=dict)
    # {(kind, namespace, name)} a Kustomization.sourceRef can resolve to: Flux source CRs
    # (GitRepository/OCIRepository/...), including the bootstrap source under
    # flux-system/, and the ExternalArtifacts ArtifactGenerators declare. Used by
    # sourceRef validation — see dependencies.check_source_references.
    flux_sources: set[tuple[str, str, str]] = field(default_factory=set)
    # Each Flux owner can render a shared directory differently (e.g. targetNamespace).
    rendered_resources: dict[str, list[K8sResource]] = field(default_factory=dict)

    # Directed graph of Flux kustomization dependencies.
    # Edge A->B means kustomization A depends on B (A must start after B is ready).
    graph: nx.DiGraph = field(init=False)

    def __post_init__(self) -> None:
        g: nx.DiGraph = nx.DiGraph()
        g.add_nodes_from(self.flux_kustomizations)
        for name, spec in self.flux_kustomizations.items():
            for dep in spec.depends_on:
                g.add_edge(name, dep.name)
        self.graph = g

    @property
    def active_flux_kustomizations(self) -> dict[str, FluxKustomizationSpec]:
        """Flux kustomizations that are not suspended."""
        return {name: spec for name, spec in self.flux_kustomizations.items() if not spec.suspend}

    def flux_kust_resources(self, *, include_suspended: bool = False) -> dict[str, list[K8sResource]]:
        """Map Flux owners to built resources; suspension retains deployed authorization objects."""
        return {
            name: resources
            for name, resources in self.rendered_resources.items()
            if not (spec := self.flux_kustomizations[name]).parked and (include_suspended or not spec.suspend)
        }


def parse_cluster(repo_root: Path) -> ParsedCluster:
    """Parse all files under the manifest roots of `repo_root` once."""
    kustomize_files: dict[Path, KustomizeFile] = {}
    flux_kustomizations: dict[str, FluxKustomizationSpec] = {}
    all_yaml_files: set[Path] = set()
    artifact_copies: dict[tuple[str, str], list[tuple[str, str]]] = {}
    flux_sources: set[tuple[str, str, str]] = set()
    flux_bootstrap_dir = repo_root / HAND_WRITTEN_ROOT / "flux" / "flux-system"

    bootstrap_files: set[Path] = set()
    for yaml_file in manifest_files(repo_root):
        # Skip blueprints directory (Authentik-specific YAML with !Env tags, not K8s resources)
        if "blueprints" in yaml_file.parts:
            continue

        if yaml_file.name in KUSTOMIZATION_FILE_NAMES:
            kustomize_files[yaml_file] = parse_kustomize_file(yaml_file)
            continue

        # Bootstrap declares sources that apps consume, but sits outside the app graph and orphan check.
        if yaml_file.is_relative_to(flux_bootstrap_dir):
            bootstrap_files.add(yaml_file.resolve())
            continue

        if yaml_file.name == "kustomizations.k8s.yaml":
            parsed_flux = parse_flux_kustomizations(yaml_file)
            parsed_active_flux = {name: spec for name, spec in parsed_flux.items() if not spec.parked}
            if parsed_flux and not parsed_active_flux:
                continue
            flux_kustomizations.update(parsed_active_flux)
        all_yaml_files.add(yaml_file.resolve())

    for spec in flux_kustomizations.values():
        if (directory := spec.local_dir(repo_root)) and directory.is_dir() and not has_kustomization_file(directory):
            kust = flux_generated_kustomization(directory)
            kustomize_files[kust.path] = kust

    # Patches and generator inputs can look like Kubernetes objects without declaring any.
    resources = {path for kust in kustomize_files.values() for path in kust.resolved_resources}
    for path in resources & (all_yaml_files | bootstrap_files):
        for resource in load_k8s_resources(path.read_bytes(), kinds=FLUX_SOURCE_KINDS | {"ArtifactGenerator"}):
            if isinstance(resource, ArtifactGeneratorResource):
                for artifact in resource.spec.artifacts:
                    name = artifact["name"]
                    flux_sources.add((EXTERNAL_ARTIFACT_KIND, resource.namespace, name))
                    artifact_copies[resource.namespace, name] = [(c["from"], c["to"]) for c in artifact.get("copy", [])]
            else:
                flux_sources.add((resource.kind, resource.namespace, resource.name))

    return ParsedCluster(
        kustomize_files=kustomize_files,
        flux_kustomizations=flux_kustomizations,
        all_yaml_files=all_yaml_files,
        artifact_copies=artifact_copies,
        flux_sources=flux_sources,
    )
