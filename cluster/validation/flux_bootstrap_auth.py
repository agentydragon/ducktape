"""Validation for Flux bootstrap git auth wiring."""

from __future__ import annotations

from pathlib import Path

from cluster.validation.k8s import GitRepositoryResource, K8sResource, parse_k8s_resource_file


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


def check_flux_bootstrap_auth(k8s_dir: Path) -> list[str]:
    """Check that cold bootstrap reads need no Flux-decrypted auth."""
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

    return errors
