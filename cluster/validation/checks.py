"""Non-graph validation checks for cluster configuration."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from cluster.validation.cluster import ParsedCluster
from cluster.validation.k8s import HelmReleaseResource, K8sResource
from cluster.validation.kustomize import KustomizeBuildResult


def find_orphaned_files(cluster: ParsedCluster, k8s_dir: Path, candidates: set[Path] | None = None) -> list[str]:
    """Find YAML files not referenced by any kustomization.

    When `candidates` is provided (pre-commit invocation), only files in that
    set are checked — keeps a clean diff from being blocked by orphans a
    parallel agent left on disk but hasn't staged yet. When `candidates` is
    None (CI / Bazel integration test) every YAML under `k8s_dir` is checked.

    Paths in `candidates` must be resolved/absolute to match `all_yaml_files`.
    """
    referenced: set[Path] = set()
    for kust in cluster.kustomize_files.values():
        referenced.update(kust.all_referenced_files)
        for resource in kust.resolved_resources:
            if resource.is_dir():
                referenced.add(resource / "kustomization.yaml")

    errors = []
    for yaml_file in cluster.all_yaml_files:
        if yaml_file.name == "kustomization.yaml":
            continue
        if candidates is not None and yaml_file not in candidates:
            continue
        if yaml_file not in referenced:
            relative = yaml_file.relative_to(k8s_dir)
            errors.append(f"Orphaned file not referenced by any kustomization: {relative}")
    return errors


def check_duplicate_external_secrets(build_results: list[KustomizeBuildResult]) -> list[str]:
    """Check for duplicate external-secrets HelmRelease installations."""
    errors = []
    deployments: dict[str, list[str]] = defaultdict(list)

    for result in build_results:
        for resource in result.resources:
            if isinstance(resource, HelmReleaseResource) and resource.name == "external-secrets":
                key = f"{resource.namespace}/{resource.chart_version or 'unknown'}"
                deployments[key].append(str(result.kustomization_path.parent))

    if len(deployments) > 1:
        errors.append("Multiple external-secrets HelmRelease found:")
        for deployment, paths in deployments.items():
            errors.append(f"  {deployment}: {', '.join(paths)}")
        errors.append("There should be exactly ONE external-secrets installation.")
    elif len(deployments) == 0:
        errors.append("No external-secrets HelmRelease found. At least one is required.")

    return errors


def check_goldilocks_namespace_labels(cluster: ParsedCluster) -> list[str]:
    """Check that namespaces with a goldilocks vpa-update-mode label also have goldilocks enabled."""
    errors = []
    for origin, resource in _goldilocks_check_resources(cluster):
        if resource.kind != "Namespace":
            continue
        labels = resource.metadata.labels
        if (
            "goldilocks.fairwinds.com/vpa-update-mode" in labels
            and labels.get("goldilocks.fairwinds.com/enabled") != "true"
        ):
            errors.append(
                f"{origin}: namespace '{resource.name}' has goldilocks.fairwinds.com/vpa-update-mode "
                f'but is missing goldilocks.fairwinds.com/enabled="true"'
            )
    return errors


_WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet"}
_GOLDILOCKS_ENABLED_LABEL = "goldilocks.fairwinds.com/enabled"


def _goldilocks_check_resources(cluster: ParsedCluster) -> list[tuple[Path, K8sResource]]:
    """Use rendered resources when available so namespace patches are included."""
    if cluster.build_results:
        return [
            (result.kustomization_path, resource) for result in cluster.build_results for resource in result.resources
        ]

    return [
        (file_path, resource) for file_path, resources in cluster.source_resources.items() for resource in resources
    ]


def check_goldilocks_explicit_decision(cluster: ParsedCluster) -> list[str]:
    """Every namespace with workloads must explicitly set goldilocks enabled label."""
    errors = []
    resources = [resource for _, resource in _goldilocks_check_resources(cluster)]

    workload_namespaces: set[str] = set()
    for resource in resources:
        if resource.kind in _WORKLOAD_KINDS and resource.namespace:
            workload_namespaces.add(resource.namespace)

    namespace_goldilocks: dict[str, str | None] = {}
    for resource in resources:
        if resource.kind == "Namespace":
            label = resource.metadata.labels.get(_GOLDILOCKS_ENABLED_LABEL)
            if label is not None or resource.name not in namespace_goldilocks:
                namespace_goldilocks[resource.name] = label

    for ns in sorted(workload_namespaces):
        if ns not in namespace_goldilocks:
            continue
        if namespace_goldilocks[ns] is None:
            errors.append(
                f"Namespace '{ns}' has workloads but is missing explicit "
                f'{_GOLDILOCKS_ENABLED_LABEL} label (set to "true" or "false")'
            )
    return errors
