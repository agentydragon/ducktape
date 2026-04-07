"""CRD layering validation — HelmReleases must not mix with CRD instances."""

from __future__ import annotations

from cluster.validation.kustomize import KustomizeBuildResult

# Operator kustomizations and the CRD kinds they manage.
# Kustomizations with empty sets are part of the operator layer but don't define CRDs.
OPERATOR_CRDS: dict[str, set[str]] = {
    "external-secrets-operator": {
        "ExternalSecret",
        "ClusterExternalSecret",
        "SecretStore",
        "ClusterSecretStore",
        "Password",
        "Fake",
        "VaultDynamicSecret",
    },
    "external-secrets": set(),
    "cert-manager": {"Certificate", "CertificateRequest", "Issuer", "ClusterIssuer"},
    "cert-manager-config": set(),
    "cert-manager-trust": set(),
    "cert-manager-environment": set(),
    "cluster-ca": set(),
    "kyverno": {"ClusterPolicy", "Policy"},
    "kyverno-policies": set(),
    "vault-operator": {"Vault"},
    "vault": set(),
    "tofu-controller": {"Terraform"},
    "powerdns-operator": {"ClusterZone", "ClusterRRset", "Zone", "RRset"},
    "monitoring-stack": {"ServiceMonitor", "PodMonitor"},
    "cnpg": {
        "Cluster",
        "Backup",
        "ScheduledBackup",
        "Pooler",
        "ClusterImageCatalog",
        "ImageCatalog",
        "Database",
        "Publication",
        "Subscription",
    },
    "longhorn": set(),  # Longhorn CRDs (Volume, Engine, etc.) are internal to the operator
    "vpa": {"VerticalPodAutoscaler", "VerticalPodAutoscalerCheckpoint"},
    "node-feature-discovery": {"NodeFeatureRule", "NodeFeature", "NodeFeatureGroup"},
    "openclaw-operator": {"OpenClawInstance", "OpenClawSelfConfig"},
    # TODO: if non-GHCR image automations are added, add a separate entry here
    # (e.g. "flux-image-automation-harbor": {"ImageRepository", ...}).
    "flux-image-automation-ghcr": {"ImageRepository", "ImagePolicy", "ImageUpdateAutomation"},
}

# Derived: CRD kind -> operator name (for error messages)
CRD_TO_OPERATOR: dict[str, str] = {kind: operator for operator, kinds in OPERATOR_CRDS.items() for kind in kinds}


class CrdLayeringViolationError(Exception):
    """Raised when a kustomization mixes HelmReleases with CRD instances."""


def check_crd_layering(result: KustomizeBuildResult) -> None:
    """Check if a kustomization mixes HelmReleases with CRD instances.

    Raises CrdLayeringViolationError if a violation is found.
    Silently returns for operator kustomizations and overlays.
    """
    if any(part in OPERATOR_CRDS for part in result.kustomization_path.parent.parts):
        return

    if "overlays" in result.kustomization_path.parts:
        return

    has_helmrelease = any(r.kind == "HelmRelease" for r in result.resources)
    crd_instances = [(r.kind, CRD_TO_OPERATOR[r.kind]) for r in result.resources if r.kind in CRD_TO_OPERATOR]

    if has_helmrelease and crd_instances:
        kust_name = result.kustomization_path.parent.name
        unique_crds = sorted({f"{k} (needs {op})" for k, op in crd_instances})
        raise CrdLayeringViolationError(
            f"{kust_name}: mixes HelmRelease with CRD instances: {', '.join(unique_crds)}. "
            f"Split into a separate '{kust_name}-secrets/' Kustomization."
        )
