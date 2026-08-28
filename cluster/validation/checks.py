"""Non-graph validation checks for cluster configuration."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from cluster.validation.cluster import ParsedCluster
from cluster.validation.k8s import (
    CiliumPolicyResource,
    CronJobResource,
    HelmReleaseResource,
    K8sResource,
    PodTemplateWorkloadResource,
    SandboxTemplateResource,
    SecretResource,
)
from cluster.validation.kustomize import KustomizeBuildResult

_FORGEJO_REGISTRY = "git.allegedly.works"
_FORGEJO_CREDENTIAL_SECRET = "forgejo-images-creds"
_REFLECTION_ALLOWED_NAMESPACES = "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"
_REFLECTION_AUTO_NAMESPACES = "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"
_FORGEJO_IMAGE_WORKLOAD_TYPES = (CronJobResource, PodTemplateWorkloadResource, SandboxTemplateResource)


def find_orphaned_files(cluster: ParsedCluster, k8s_dir: Path) -> list[str]:
    """Find YAML files not referenced by any kustomization."""
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
    for origin, resource in _rendered_or_source_resources(cluster):
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
    """Every rendered Forgejo image workload namespace is covered by the reflected pull secret."""
    credential_annotations = {
        (
            resource.metadata.annotations.get(_REFLECTION_ALLOWED_NAMESPACES, ""),
            resource.metadata.annotations.get(_REFLECTION_AUTO_NAMESPACES, ""),
        )
        for _, resource in _rendered_or_source_resources(cluster)
        if isinstance(resource, SecretResource)
        and resource.name == _FORGEJO_CREDENTIAL_SECRET
        and resource.namespace == "forgejo-images"
    }
    if not credential_annotations:
        return [f"Secret forgejo-images/{_FORGEJO_CREDENTIAL_SECRET} is not present in rendered resources"]
    if len(credential_annotations) > 1:
        return [f"Secret forgejo-images/{_FORGEJO_CREDENTIAL_SECRET} has inconsistent reflection allowlists"]

    allowed, auto = next(iter(credential_annotations))
    allowlists = {
        _REFLECTION_ALLOWED_NAMESPACES: {item.strip() for item in allowed.split(",") if item.strip()},
        _REFLECTION_AUTO_NAMESPACES: {item.strip() for item in auto.split(",") if item.strip()},
    }
    errors: list[str] = []
    for origin, resource in _rendered_or_source_resources(cluster):
        if not isinstance(resource, _FORGEJO_IMAGE_WORKLOAD_TYPES):
            continue
        images = _forgejo_images(resource)
        if not images:
            continue
        for annotation, namespaces in allowlists.items():
            if resource.namespace not in namespaces:
                errors.append(
                    f"{origin}: {resource.kind} '{resource.namespace}/{resource.name}' runs Forgejo image(s) "
                    f"{', '.join(sorted(images))}, but namespace '{resource.namespace}' is missing from "
                    f"{annotation} on Secret forgejo-images/{_FORGEJO_CREDENTIAL_SECRET}"
                )
    return errors


def check_goldilocks_explicit_decision(cluster: ParsedCluster) -> list[str]:
    """Every namespace with workloads must explicitly set goldilocks enabled label."""
    errors = []
    resources = [resource for _, resource in _rendered_or_source_resources(cluster)]

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


def check_sops_decryption_blocks(cluster: ParsedCluster, k8s_dir: Path) -> list[str]:
    """Active Flux Kustomizations that render a SOPS-encrypted Secret must declare
    spec.decryption with provider: sops AND a secretRef.name — otherwise Flux applies
    the ENC[...] ciphertext literally (no provider) or has no age key to decrypt with
    (no secretRef). Both fail silently. Build-level: inspects what Flux actually
    applies, so it neither over-counts SOPS files in sibling/child kustomizations nor
    misses those pulled in via nested kustomize refs."""
    errors: list[str] = []
    for name, resources in cluster.flux_kust_resources(k8s_dir).items():
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
    """Every Cilium policy rule must have a non-empty rule section.

    Mirrors Cilium's `Rule.Sanitize`: a rule whose `ingress`, `ingressDeny`, `egress`
    and `egressDeny` are all empty is schema-valid but rejected at import
    ("rule must have at least one of Ingress, IngressDeny, Egress, EgressDeny"),
    leaving the policy `Valid=False` and silently unenforced — a fail-open shape that
    widens exposure instead of breaking traffic (#4923). Default-deny is spelled as a
    single empty rule element (`ingress: [{}]`), which allows nothing while putting
    selected endpoints into default deny."""
    errors = []
    for origin, resource in _rendered_or_source_resources(cluster):
        if not isinstance(resource, CiliumPolicyResource):
            continue
        for index, rule in enumerate(resource.rules):
            if not (rule.ingress or rule.ingress_deny or rule.egress or rule.egress_deny):
                errors.append(
                    f"{origin}: {resource.kind} '{resource.name}' rule {index} has no non-empty "
                    f"ingress/ingressDeny/egress/egressDeny section — Cilium rejects it (Valid=False) "
                    f"and enforces nothing. For default-deny use one empty rule element, e.g. "
                    f"`ingress: [{{}}]`, never an empty list."
                )
    return errors
