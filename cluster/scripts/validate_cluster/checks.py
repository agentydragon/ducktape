"""Non-graph validation checks for cluster configuration."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import yaml

from cluster.validation.cluster import ParsedCluster
from cluster.validation.crd_layering import CrdLayeringViolationError, check_crd_layering as _check_crd_layering_raises
from cluster.validation.health_checks import check_controller_health_checks
from cluster.validation.kustomize import KustomizeBuildResult


def find_orphaned_files(cluster: ParsedCluster, k8s_dir: Path) -> list[str]:
    """Find YAML files not referenced by any kustomization."""
    errors = []

    # Build set of all referenced files
    referenced: set[Path] = set()
    for kust in cluster.kustomize_files.values():
        referenced.update(kust.resources)
        referenced.update(kust.patches)
        # If resource is a directory, also mark its kustomization.yaml
        for resource in kust.resources:
            if resource.is_dir():
                referenced.add(resource / "kustomization.yaml")

    for yaml_file in cluster.all_yaml_files:
        if yaml_file.name == "kustomization.yaml":
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
            if resource.kind == "HelmRelease" and resource.name == "external-secrets":
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


def check_crd_layering(result: KustomizeBuildResult) -> list[str]:
    """Check CRD layering, returning errors as strings (adapter for raising API)."""
    try:
        _check_crd_layering_raises(result)
    except CrdLayeringViolationError as e:
        return [str(e)]
    return []


def check_controller_resource_health_checks(cluster: ParsedCluster, k8s_dir: Path, repo_root: Path) -> list[str]:
    """Check that flux kustomizations deploying controller resources have healthChecks."""
    return check_controller_health_checks(cluster, k8s_dir, repo_root)


def check_goldilocks_namespace_labels(cluster: ParsedCluster) -> list[str]:
    """Check that namespaces with a goldilocks vpa-update-mode label also have goldilocks enabled."""
    errors = []
    for file_path, resources in cluster.source_resources.items():
        for resource in resources:
            if resource.kind != "Namespace":
                continue
            labels = resource.metadata.labels
            if "goldilocks.fairwinds.com/vpa-update-mode" in labels and labels.get("goldilocks.fairwinds.com/enabled") != "true":
                errors.append(
                    f"{file_path}: namespace '{resource.name}' has goldilocks.fairwinds.com/vpa-update-mode "
                    f"but is missing goldilocks.fairwinds.com/enabled=\"true\""
                )
    return errors


def check_blueprint_completeness(k8s_dir: Path) -> list[str]:
    """Check that all blueprint YAML files are listed in the authentik configMapGenerator."""
    authentik_kust = k8s_dir / "authentik" / "kustomization.yaml"
    blueprints_dir = k8s_dir / "authentik" / "blueprints"

    if not authentik_kust.exists() or not blueprints_dir.exists():
        return []

    with authentik_kust.open() as f:
        doc = yaml.safe_load(f)

    listed_files: set[str] = set()
    for generator in doc.get("configMapGenerator", []):
        if generator.get("name") == "authentik-sso-blueprints":
            listed_files = {Path(f).name for f in generator.get("files", [])}
            break

    on_disk = {p.name for p in blueprints_dir.glob("*.yaml")}
    unlisted = sorted(on_disk - listed_files)

    if unlisted:
        return [
            f"Authentik blueprint not listed in configMapGenerator: {name}. "
            f"Add 'blueprints/{name}' to the authentik-sso-blueprints files list "
            f"in k8s/authentik/kustomization.yaml."
            for name in unlisted
        ]

    return []
