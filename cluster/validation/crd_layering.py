"""CRD provider prerequisites for Flux dependency validation."""

from __future__ import annotations

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
    "cert-manager-trust": {"Bundle"},
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
