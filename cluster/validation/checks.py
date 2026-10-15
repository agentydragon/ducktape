"""Non-graph validation checks for cluster configuration."""

from __future__ import annotations

from pathlib import Path

from cluster.cdk8s.cilium_rule_sections import enforces_nothing
from cluster.validation.cluster import ParsedCluster
from cluster.validation.k8s import (
    CiliumPolicyResource,
    CronJobResource,
    ExternalSecretResource,
    K8sResource,
    PodTemplateWorkloadResource,
    SandboxTemplateResource,
    SecretResource,
    SecretStoreResource,
)

_FORGEJO_REGISTRY = "git.allegedly.works"
_FORGEJO_CREDENTIAL_SECRET = "forgejo-images-creds"
_FORGEJO_IMAGE_WORKLOAD_TYPES = (CronJobResource, PodTemplateWorkloadResource, SandboxTemplateResource)
# These inputs are intentionally stored without a deploy Kustomization reference:
# Dreo is retained for future Home Assistant provisioning (see homeassistant/TODO.md).
_INTENTIONALLY_STORED_ONLY_FILES = frozenset({Path("cluster/k8s/external-creds/dreo-account.sops.yaml")})


def find_orphaned_files(cluster: ParsedCluster, repo_root: Path) -> list[str]:
    """Find YAML files not referenced by any kustomization, except stored-only inputs."""
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
        relative = yaml_file.relative_to(repo_root)
        if relative in _INTENTIONALLY_STORED_ONLY_FILES:
            continue
        if yaml_file not in referenced:
            errors.append(f"Orphaned file not referenced by any kustomization: {relative}")
    return errors


def check_external_credential_ownership(cluster: ParsedCluster, repo_root: Path) -> list[str]:
    """Only the shared external-creds ClusterSecretStore reads ducktape-flux."""
    store_owner = "external-secrets-config"
    store_name = "kubernetes-external-creds-secret-store"
    resources_by_kustomization = cluster.flux_kust_resources(repo_root)
    errors: list[str] = []
    stores: list[SecretStoreResource] = []
    for kustomization, resources in resources_by_kustomization.items():
        for resource in resources:
            if not isinstance(resource, SecretStoreResource):
                continue
            provider = resource.spec.provider.kubernetes
            if provider is None or provider.remote_namespace != "ducktape-flux":
                continue
            if resource.kind != "ClusterSecretStore" or resource.name != store_name or kustomization != store_owner:
                errors.append(
                    f"{kustomization} {resource.kind} '{resource.namespace}/{resource.name}' reads "
                    f"external-creds; use {store_owner}'s shared ClusterSecretStore '{store_name}'"
                )
            else:
                stores.append(resource)

    if len(stores) != 1:
        errors.append(f"expected exactly one {store_owner} ClusterSecretStore '{store_name}', found {len(stores)}")

    return errors


def check_goldilocks_namespace_labels(cluster: ParsedCluster) -> list[str]:
    """A namespace with a Goldilocks update mode is not opted out of Goldilocks, which would ignore
    the mode. Goldilocks runs on by default, so the mode needs no `enabled: "true"` beside it."""
    errors = []
    for origin, resource in _rendered_or_source_resources(cluster):
        if resource.kind != "Namespace":
            continue
        labels = resource.metadata.labels
        if _VPA_UPDATE_MODE_LABEL in labels and labels.get(_GOLDILOCKS_ENABLED_LABEL, "true") != "true":
            errors.append(
                f"{origin}: namespace '{resource.name}' has {_VPA_UPDATE_MODE_LABEL} "
                f'but is labeled {_GOLDILOCKS_ENABLED_LABEL}: "{labels[_GOLDILOCKS_ENABLED_LABEL]}"'
            )
    return errors


_GOLDILOCKS_ENABLED_LABEL = "goldilocks.fairwinds.com/enabled"
_VPA_UPDATE_MODE_LABEL = "goldilocks.fairwinds.com/vpa-update-mode"


def _rendered_or_source_resources(cluster: ParsedCluster) -> list[tuple[Path, K8sResource]]:
    """Use rendered resources when available so patches are included."""
    if cluster.build_results:
        return [
            (result.kustomization_path, resource) for result in cluster.build_results for resource in result.resources
        ]

    return [
        (file_path, resource) for file_path, resources in cluster.source_resources.items() for resource in resources
    ]


def _forgejo_images(resource: CronJobResource | PodTemplateWorkloadResource | SandboxTemplateResource) -> set[str]:
    return {
        image
        for pod_spec in resource.pod_specs
        for image in pod_spec.images
        if image.split("/", 1)[0] == _FORGEJO_REGISTRY
    }


def check_forgejo_image_namespace_reflection(cluster: ParsedCluster) -> list[str]:
    """Every rendered Forgejo image workload namespace has its own forgejo-images-creds ExternalSecret."""
    namespaces_with_credential = {
        resource.namespace
        for _, resource in _rendered_or_source_resources(cluster)
        if isinstance(resource, ExternalSecretResource) and resource.name == _FORGEJO_CREDENTIAL_SECRET
    }
    errors: list[str] = []
    for origin, resource in _rendered_or_source_resources(cluster):
        if not isinstance(resource, _FORGEJO_IMAGE_WORKLOAD_TYPES):
            continue
        images = _forgejo_images(resource)
        if not images:
            continue
        if resource.namespace not in namespaces_with_credential:
            errors.append(
                f"{origin}: {resource.kind} '{resource.namespace}/{resource.name}' runs Forgejo image(s) "
                f"{', '.join(sorted(images))}, but namespace '{resource.namespace}' has no "
                f"ExternalSecret named {_FORGEJO_CREDENTIAL_SECRET}"
            )
    return errors


def check_sops_decryption_blocks(cluster: ParsedCluster, repo_root: Path) -> list[str]:
    """Active Flux Kustomizations that render a SOPS-encrypted Secret must declare
    spec.decryption with provider: sops AND a secretRef.name — otherwise Flux applies
    the ENC[...] ciphertext literally (no provider) or has no age key to decrypt with
    (no secretRef). Both fail silently. Build-level: inspects what Flux actually
    applies, so it neither over-counts SOPS files in sibling/child kustomizations nor
    misses those pulled in via nested kustomize refs."""
    errors: list[str] = []
    for name, resources in cluster.flux_kust_resources(repo_root).items():
        if not any(isinstance(r, SecretResource) and r.sops is not None for r in resources):
            continue
        spec = cluster.active_flux_kustomizations[name]
        dec = spec.decryption
        if dec is None or dec.provider != "sops":
            errors.append(
                f"Flux Kustomization '{name}' renders a SOPS-encrypted Secret but has no "
                f"spec.decryption.provider: sops. Without it Flux applies the ENC[...] "
                f"ciphertext literally — add a decryption block pointing at sops-age-cluster-secrets."
            )
        elif dec.secret_ref is None or not dec.secret_ref.name:
            errors.append(
                f"Flux Kustomization '{name}' declares decryption.provider: sops but no "
                f"spec.decryption.secretRef.name — Flux has no age key to decrypt with, so the "
                f"Secret's ENC[...] ciphertext is applied literally. Add a secretRef pointing at "
                f"sops-age-cluster-secrets."
            )
    return errors


def check_cilium_policy_rules_nonempty(cluster: ParsedCluster) -> list[str]:
    """No Cilium policy rule `enforces_nothing`. The fleet rules refuse a generated one at
    synth; this holds hand-written policies (`haku/x/dispatch/deploy`'s) and anything a kustomize
    patch changes after synth."""
    errors = []
    for origin, resource in _rendered_or_source_resources(cluster):
        if not isinstance(resource, CiliumPolicyResource):
            continue
        for index, rule in enumerate(resource.rules):
            if enforces_nothing(rule):
                errors.append(
                    f"{origin}: {resource.kind} '{resource.name}' rule {index} has no non-empty "
                    f"ingress/ingressDeny/egress/egressDeny section — Cilium rejects it (Valid=False) "
                    f"and enforces nothing. For default-deny use one empty rule element, e.g. "
                    f"`ingress: [{{}}]`, never an empty list."
                )
    return errors
