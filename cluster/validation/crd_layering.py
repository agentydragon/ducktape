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
    "tofu-controller": {"Terraform"},
    # The CRDs, not the HelmRelease: prometheus-operator's only admission webhooks
    # are failurePolicy: Ignore and cover prometheusrules/alertmanagerconfigs, so
    # the prerequisite for any of these kinds is the CRD existing, never Prometheus
    # running. Gating a ServiceMonitor on monitoring-stack withheld a component's
    # scrape config exactly when Prometheus was unhealthy.
    "monitoring-crds": {
        "Alertmanager",
        "AlertmanagerConfig",
        "PodMonitor",
        "Probe",
        "Prometheus",
        "PrometheusAgent",
        "PrometheusRule",
        "ScrapeConfig",
        "ServiceMonitor",
        "ThanosRuler",
    },
    "monitoring-stack": set(),
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
    "vpa": {"VerticalPodAutoscaler", "VerticalPodAutoscalerCheckpoint"},
    "node-feature-discovery": {"NodeFeatureRule", "NodeFeature", "NodeFeatureGroup"},
    "kubevirt-operator": {"KubeVirt"},
    "kubevirt": {
        "VirtualMachine",
        "VirtualMachineClone",
        "VirtualMachineExport",
        "VirtualMachineInstance",
        "VirtualMachineInstanceMigration",
        "VirtualMachineInstancePreset",
        "VirtualMachineInstanceReplicaSet",
        "VirtualMachinePool",
        "VirtualMachineRestore",
        "VirtualMachineSnapshot",
        "VirtualMachineSnapshotContent",
    },
    "cdi-operator": {"CDI"},
    "cdi": {
        "CDIConfig",
        "DataImportCron",
        "DataSource",
        "DataVolume",
        "ObjectTransfer",
        "StorageProfile",
        "VolumeCloneSource",
        "VolumeImportSource",
        "VolumeSnapshotSource",
        "VolumeUploadSource",
    },
    "openclaw-operator": {"OpenClawInstance", "OpenClawSelfConfig"},
    "agentplane-crds": {"EgressPolicy", "EgressBinding", "ActionPolicySet", "ActionPolicyBinding"},
    "sshpiper-crds": {"Pipe"},
    "seaweedfs-operator": {"Bucket", "S3Identity", "S3Credentials", "ResourceReferenceGrant"},
    # TODO: if non-GHCR image automations are added, add a separate entry here
    # (e.g. "flux-image-automation-dockerhub": {"ImageRepository", ...}).
    "flux-image-automation-ghcr": {"ImageRepository", "ImagePolicy", "ImageUpdateAutomation"},
}

# Derived: CRD kind -> operator name (for error messages)
CRD_TO_OPERATOR: dict[str, str] = {kind: operator for operator, kinds in OPERATOR_CRDS.items() for kind in kinds}

# Suppressions for an over-broad check, not a record of real exceptions.
#
# TODO: narrow check_crd_layering to the hazard it is named for, then delete this
# set. It fires on any HelmRelease beside any CR, as though every chart could
# install every CRD. The hazard is narrower: a chart that installs a CRD, with
# instances of *that* CRD in the same Kustomization, which would apply before the
# chart had created it. Every path below is the benign shape instead — an
# unrelated chart next to a CR whose CRD comes from another component entirely
# (checked 2026-09-16: forgejo/loki/mimir/tempo carry SeaweedFS Bucket and
# S3Identity, authentik/gatus/forgejo carry prometheus-operator monitors, and no
# chart here installs any of them). Doing it properly needs a HelmRelease → the
# CRDs its chart installs mapping, which nothing in the repo has yet.
#
# validate_operator_dependencies is unaffected and still requires each of these to
# reach the operator that serves its CRDs.
MIXED_CRD_LAYERING_EXCEPTIONS = {
    "authentik/app",
    "forgejo/app",
    "gatus/app",
    "monitoring/loki",
    "monitoring/mimir",
    "monitoring/tempo",
}


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

    kustomization_dir = result.kustomization_path.parent.as_posix()
    if any(kustomization_dir.endswith(f"/{path}") for path in MIXED_CRD_LAYERING_EXCEPTIONS):
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
