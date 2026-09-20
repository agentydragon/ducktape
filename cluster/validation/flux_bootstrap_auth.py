"""Validation for Flux bootstrap and image-automation git auth wiring."""

from __future__ import annotations

from pathlib import Path

from cluster.validation.cluster import ParsedCluster
from cluster.validation.k8s import (
    GitRepositoryResource,
    ImageUpdateAutomationResource,
    K8sResource,
    parse_k8s_resource_file,
)


def _resource_key(resource: K8sResource) -> tuple[str, str]:
    return (resource.namespace, resource.name)


def _sops_managed_secret_keys(k8s_dir: Path) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for sops_file in k8s_dir.rglob("*.sops.yaml"):
        for resource in parse_k8s_resource_file(sops_file):
            if resource.kind == "Secret":
                keys.add(_resource_key(resource))
    return keys


def _bootstrap_gitrepositories(k8s_dir: Path) -> list[GitRepositoryResource]:
    bootstrap_sync = k8s_dir / "flux" / "flux-system" / "gotk-sync.yaml"
    if not bootstrap_sync.exists():
        return []
    return [
        resource for resource in parse_k8s_resource_file(bootstrap_sync) if isinstance(resource, GitRepositoryResource)
    ]


def check_flux_bootstrap_auth(cluster: ParsedCluster, k8s_dir: Path) -> list[str]:
    """Check that cold bootstrap reads are public and runtime write sources are authenticated."""
    errors: list[str] = []
    sops_secrets = _sops_managed_secret_keys(k8s_dir)

    for source in _bootstrap_gitrepositories(k8s_dir):
        if source.spec.provider == "github":
            errors.append(
                f"Raw bootstrap GitRepository '{source.namespace}/{source.name}' sets provider=github. "
                "Terraform applies gotk-sync.yaml before Flux can decrypt SOPS Secrets; keep bootstrap "
                "sources anonymous or use a Secret created before source-controller fetches the repo."
            )
        if source.spec.secret_ref and (source.namespace, source.spec.secret_ref.name) in sops_secrets:
            errors.append(
                f"Raw bootstrap GitRepository '{source.namespace}/{source.name}' references SOPS-managed "
                f"Secret '{source.namespace}/{source.spec.secret_ref.name}'. That creates a cold-start "
                "cycle because source-controller needs git auth before kustomize-controller can decrypt "
                "and apply the Secret."
            )

    git_repositories: dict[tuple[str, str], GitRepositoryResource] = {}
    image_automations: list[ImageUpdateAutomationResource] = []
    for result in cluster.build_results:
        for resource in result.resources:
            if isinstance(resource, GitRepositoryResource):
                git_repositories[_resource_key(resource)] = resource
            elif isinstance(resource, ImageUpdateAutomationResource):
                image_automations.append(resource)

    for automation in image_automations:
        source_ref = automation.spec.source_ref
        if source_ref.kind != "GitRepository":
            continue
        source_namespace = source_ref.namespace or automation.namespace
        source_key = (source_namespace, source_ref.name)
        referenced_source = git_repositories.get(source_key)
        if referenced_source is None:
            errors.append(
                f"ImageUpdateAutomation '{automation.namespace}/{automation.name}' references missing "
                f"GitRepository '{source_namespace}/{source_ref.name}'."
            )
            continue
        if referenced_source.spec.secret_ref is None or not referenced_source.spec.secret_ref.name:
            errors.append(
                f"ImageUpdateAutomation '{automation.namespace}/{automation.name}' uses GitRepository "
                f"'{referenced_source.namespace}/{referenced_source.name}' without a secretRef. Image "
                "automation writes commits back to git, so the referenced source must carry push-capable "
                "credentials instead of reusing the anonymous bootstrap source."
            )

    return errors
