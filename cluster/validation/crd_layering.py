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
    # The HelmRelease in this Kustomization installs ExternalDNS's DNSEndpoint CRD.
    "external-dns": {"DNSEndpoint"},
    "cert-manager": {"Certificate", "CertificateRequest", "Issuer", "ClusterIssuer"},
    "cert-manager-config": set(),
    "cert-manager-trust": {"Bundle"},
    "cert-manager-environment": set(),
    "cluster-ca": set(),
    "keda": {"ScaledJob", "TriggerAuthentication"},
    "kyverno": {"ClusterPolicy", "Policy", "CleanupPolicy"},
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
        # From the unit's plugin-barman-cloud HelmRelease.
        "ObjectStore",
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
    "agentplane-crds": {"EgressPolicy", "EgressBinding", "EgressCredential", "ActionPolicySet", "ActionPolicyBinding"},
    "agent-sandbox-controller": {"SandboxTemplate", "SandboxWarmPool"},
    "sshpiper-crds": {"Pipe"},
    "seaweedfs-operator": {
        "AdminScript",
        "Bucket",
        "ResourceReferenceGrant",
        "S3Credentials",
        "S3Identity",
        "S3Policy",
        "S3PolicyBinding",
        "Seaweed",
    },
    # From the unit's redis-operator HelmRelease.
    "valkey": {"RedisReplication"},
    "grafana-operator": {"Grafana", "GrafanaDashboard", "GrafanaDatasource", "GrafanaServiceAccount"},
    "clickhouse-operator": {"ClickHouseInstallation", "ClickHouseKeeperInstallation"},
    "volsync": {"ReplicationDestination", "ReplicationSource"},
    "snapshot-controller-crds": {"VolumeSnapshotClass"},
    # TODO: if non-GHCR image automations are added, add a separate entry here
    # (e.g. "flux-image-automation-dockerhub": {"ImageRepository", ...}).
    "flux-image-automation-ghcr": {"ImageRepository", "ImagePolicy", "ImageUpdateAutomation"},
}

# Derived: CRD kind -> operator name (for error messages)
CRD_TO_OPERATOR: dict[str, str] = {kind: operator for operator, kinds in OPERATOR_CRDS.items() for kind in kinds}

# API groups the Kubernetes API server serves without a CRD.
BUILT_IN_API_GROUPS = frozenset(
    {
        "",
        "admissionregistration.k8s.io",
        "apiextensions.k8s.io",
        "apiregistration.k8s.io",
        "apps",
        "authentication.k8s.io",
        "authorization.k8s.io",
        "autoscaling",
        "batch",
        "certificates.k8s.io",
        "coordination.k8s.io",
        "discovery.k8s.io",
        "events.k8s.io",
        "flowcontrol.apiserver.k8s.io",
        "internal.apiserver.k8s.io",
        "networking.k8s.io",
        "node.k8s.io",
        "policy",
        "rbac.authorization.k8s.io",
        "resource.k8s.io",
        "scheduling.k8s.io",
        "storage.k8s.io",
        "storagemigration.k8s.io",
    }
)

# API groups whose CRDs exist before any Kustomization in the graph applies. A rendered
# kind outside these groups, BUILT_IN_API_GROUPS and OPERATOR_CRDS fails validation.
BOOTSTRAP_API_GROUPS = frozenset(
    {
        # Flux, from `flux bootstrap` (cluster/k8s/flux/flux-system/gotk-components.yaml).
        "helm.toolkit.fluxcd.io",
        "image.toolkit.fluxcd.io",
        "kustomize.toolkit.fluxcd.io",
        "notification.toolkit.fluxcd.io",
        "source.extensions.fluxcd.io",
        "source.toolkit.fluxcd.io",
        # Cilium, which registers its own CRDs, and the Gateway API CRDs it requires: both
        # from cluster/terraform/main/cilium.tf.
        "cilium.io",
        "gateway.networking.k8s.io",
    }
)
