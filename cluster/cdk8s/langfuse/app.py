"""Langfuse: its namespace, Postgres, S3 bucket and credentials, route, log-reader RBAC,
queue/cache Valkey and Helm release, and the `langfuse` Flux Kustomization owning them.

Hand-written beside the generated output: `langfuse-secrets.sops.yaml`.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from cnpg_cluster_crds.io.cnpg.postgresql import (
    Cluster,
    ClusterSpec,
    ClusterSpecAffinity,
    ClusterSpecAffinityTolerations,
    ClusterSpecBootstrap,
    ClusterSpecBootstrapInitdb,
    ClusterSpecMonitoring,
    ClusterSpecProbes,
    ClusterSpecProbesLiveness,
    ClusterSpecProbesLivenessIsolationCheck,
    ClusterSpecStorage,
)
from constructs import Construct
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmRelease,
    HelmReleaseSpec,
    HelmReleaseSpecChart,
    HelmReleaseSpecChartSpec,
    HelmReleaseSpecChartSpecSourceRef,
    HelmReleaseSpecChartSpecSourceRefKind,
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallStrategy,
    HelmReleaseSpecInstallStrategyName,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecUpgradeStrategy,
    HelmReleaseSpecUpgradeStrategyName,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from redis_operator_redisreplication_crds.in_.opstreelabs.redis.redis import (
    RedisReplication,
    RedisReplicationSpec,
    RedisReplicationSpecAffinity,
    RedisReplicationSpecAffinityNodeAffinity,
    RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecution,
    RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreference,
    RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreferenceMatchExpressions,
    RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution,
    RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms,
    RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions,
    RedisReplicationSpecAffinityPodAntiAffinity,
    RedisReplicationSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecution,
    RedisReplicationSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecutionLabelSelector,
    RedisReplicationSpecKubernetesConfig,
    RedisReplicationSpecKubernetesConfigResources,
    RedisReplicationSpecKubernetesConfigResourcesLimits,
    RedisReplicationSpecKubernetesConfigResourcesRequests,
    RedisReplicationSpecRedisConfig,
    RedisReplicationSpecStorage,
    RedisReplicationSpecStorageVolumeClaimTemplate,
    RedisReplicationSpecStorageVolumeClaimTemplateSpec,
    RedisReplicationSpecStorageVolumeClaimTemplateSpecResources,
    RedisReplicationSpecStorageVolumeClaimTemplateSpecResourcesRequests,
)
from seaweed_bucket_crds.com.seaweedfs.seaweed import (
    Bucket,
    BucketSpec,
    BucketSpecAccess,
    BucketSpecAccessActions,
    BucketSpecClusterRef,
    BucketSpecReclaimPolicy,
)
from seaweed_resourcereferencegrant_crds.com.seaweedfs.seaweed import (
    ResourceReferenceGrant,
    ResourceReferenceGrantSpec,
    ResourceReferenceGrantSpecFrom,
    ResourceReferenceGrantSpecTo,
)
from seaweed_s3credentials_crds.com.seaweedfs.seaweed import (
    S3Credentials,
    S3CredentialsSpec,
    S3CredentialsSpecIdentityRef,
    S3CredentialsSpecReclaimPolicy,
    S3CredentialsSpecSeaweedRef,
    S3CredentialsSpecSecretRef,
)
from seaweed_s3identity_crds.com.seaweedfs.seaweed import (
    S3Identity,
    S3IdentitySpec,
    S3IdentitySpecReclaimPolicy,
    S3IdentitySpecSeaweedRef,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.cnpg import OFF_CONTROL_PLANE_NODE_AFFINITY
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.metadata import metadata

OUTPUT_DIR = "cluster/k8s/langfuse"
_NAME = "langfuse"
_NAMESPACE = "langfuse"
_ZONE = "hil-ovh"
_SEAWEEDFS = "seaweedfs"
_SEAWEED_GROUP = "seaweed.seaweedfs.com"
_S3_CREDENTIALS_SECRET = "langfuse-seaweedfs-credentials"
_VALKEY = "langfuse-valkey-ovh"


def _namespace(scope: Construct) -> None:
    k8s.KubeNamespace(
        scope,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=_NAMESPACE,
            labels={"goldilocks.fairwinds.com/enabled": "true", "goldilocks.fairwinds.com/vpa-update-mode": "auto"},
        ),
    )


def _database(scope: Construct) -> None:
    Cluster(
        scope,
        "database",
        metadata=metadata("langfuse-db", _NAMESPACE),
        spec=ClusterSpec(
            instances=2,
            image_name="ghcr.io/cloudnative-pg/postgresql:18.1-system-trixie",
            # CNPG 1.27+ kills isolated primaries by default (liveness probe).
            # Disable to prevent false positives from transient network blips.
            probes=ClusterSpecProbes(
                liveness=ClusterSpecProbesLiveness(
                    isolation_check=ClusterSpecProbesLivenessIsolationCheck(enabled=False)
                )
            ),
            affinity=ClusterSpecAffinity(
                node_selector={"topology.kubernetes.io/zone": _ZONE},
                tolerations=[
                    ClusterSpecAffinityTolerations(
                        key="node-role.kubernetes.io/control-plane", operator="Exists", effect="NoSchedule"
                    )
                ],
                topology_key="kubernetes.io/hostname",
                node_affinity=OFF_CONTROL_PLANE_NODE_AFFINITY,
            ),
            storage=ClusterSpecStorage(storage_class="local-path-ovh", size="10Gi"),
            monitoring=ClusterSpecMonitoring(enable_pod_monitor=True),
            # CNPG auto-generates credentials in secret langfuse-db-app
            bootstrap=ClusterSpecBootstrap(initdb=ClusterSpecBootstrapInitdb(database="langfuse", owner="langfuse")),
        ),
    )


def _storage(scope: Construct) -> None:
    # Retain the previous credential Secret during the staged handoff. The old
    # S3Credentials resource is retired separately; revoking its retained key and
    # removing this rollback Secret is an explicit follow-up.
    k8s.KubeSecret(
        scope,
        "legacy-s3-credentials",
        metadata=k8s.ObjectMeta(
            name="langfuse-s3-credentials",
            namespace=_NAMESPACE,
            annotations={"kustomize.toolkit.fluxcd.io/ssa": "Merge"},
        ),
        type="Opaque",
    )
    Bucket(
        scope,
        "bucket",
        metadata=metadata(_NAME, _NAMESPACE, annotations={"description": "Langfuse event, export, and media objects."}),
        spec=BucketSpec(
            name=_NAME,
            # The physical bucket is populated; adopt it during the ownership handoff.
            adopt_existing=True,
            cluster_ref=BucketSpecClusterRef(name=_SEAWEEDFS, namespace=_SEAWEEDFS),
            reclaim_policy=BucketSpecReclaimPolicy.RETAIN,
            access=[
                BucketSpecAccess(
                    user=_NAME,
                    actions=[
                        BucketSpecAccessActions.READ,
                        BucketSpecAccessActions.WRITE,
                        BucketSpecAccessActions.LIST,
                        BucketSpecAccessActions.TAGGING,
                    ],
                )
            ],
        ),
    )
    S3Credentials(
        scope,
        "s3-credentials",
        metadata=metadata(
            _NAME, _NAMESPACE, annotations={"description": "Langfuse's tenant-local SeaweedFS credentials."}
        ),
        spec=S3CredentialsSpec(
            seaweed_ref=S3CredentialsSpecSeaweedRef(name=_SEAWEEDFS, namespace=_SEAWEEDFS),
            # The IAM identity name is cluster-global. Without a same-namespace
            # S3Identity, the operator uses the existing identity named langfuse.
            identity_ref=S3CredentialsSpecIdentityRef(name=_NAME),
            # Use a new Secret during the staged handoff. The existing Secret is
            # populated by the old cross-namespace S3Credentials object and cannot be
            # adopted here.
            secret_ref=S3CredentialsSpecSecretRef(
                name=_S3_CREDENTIALS_SECRET,
                access_key_field="s3-access-key-id",
                secret_key_field="s3-secret-access-key",
            ),
            reclaim_policy=S3CredentialsSpecReclaimPolicy.RETAIN,
        ),
    )
    # Permit only Langfuse's tenant-local Bucket and S3Credentials to reference the
    # SeaweedFS cluster in its namespace.
    ResourceReferenceGrant(
        scope,
        "reference-grant",
        metadata=metadata(_NAME, _SEAWEEDFS),
        spec=ResourceReferenceGrantSpec(
            from_=[
                ResourceReferenceGrantSpecFrom(group=_SEAWEED_GROUP, kind="Bucket", namespace=_NAMESPACE),
                ResourceReferenceGrantSpecFrom(group=_SEAWEED_GROUP, kind="S3Credentials", namespace=_NAMESPACE),
            ],
            to=[ResourceReferenceGrantSpecTo(group=_SEAWEED_GROUP, kind="Seaweed", name=_SEAWEEDFS)],
        ),
    )
    S3Identity(
        scope,
        "s3-identity",
        metadata=metadata(_NAME, _SEAWEEDFS),
        spec=S3IdentitySpec(
            seaweed_ref=S3IdentitySpecSeaweedRef(name=_SEAWEEDFS), reclaim_policy=S3IdentitySpecReclaimPolicy.RETAIN
        ),
    )


def _log_reader(scope: Construct) -> None:
    role = k8s.KubeRole(
        scope,
        "log-reader",
        metadata=k8s.ObjectMeta(name="langfuse-log-reader", namespace=_NAMESPACE),
        rules=[
            k8s.PolicyRule(
                api_groups=[""],
                resources=["pods", "pods/log", "services", "configmaps", "events"],
                verbs=["get", "list", "watch"],
            )
        ],
    )
    k8s.KubeRoleBinding(
        scope,
        "log-reader-binding",
        metadata=k8s.ObjectMeta(name="claude-langfuse-reader", namespace=_NAMESPACE),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind=role.kind, name=role.name),
        subjects=[
            k8s.Subject(kind="ServiceAccount", name="default", namespace="claude-sandbox"),
            k8s.Subject(
                kind="Group", name="oidc-ksbx-groups:kubectl-sandbox-users", api_group="rbac.authorization.k8s.io"
            ),
        ],
    )


def _valkey(scope: Construct) -> None:
    RedisReplication(
        scope,
        "valkey",
        metadata=metadata(
            _VALKEY, _NAMESPACE, annotations={"description": "OVH Valkey for Langfuse queue/cache state"}
        ),
        spec=RedisReplicationSpec(
            cluster_size=2,
            kubernetes_config=RedisReplicationSpecKubernetesConfig(
                image="valkey/valkey:9-alpine",
                image_pull_policy="IfNotPresent",
                resources=RedisReplicationSpecKubernetesConfigResources(
                    requests={
                        "cpu": RedisReplicationSpecKubernetesConfigResourcesRequests.from_string("50m"),
                        "memory": RedisReplicationSpecKubernetesConfigResourcesRequests.from_string("128Mi"),
                    },
                    limits={
                        "cpu": RedisReplicationSpecKubernetesConfigResourcesLimits.from_string("500m"),
                        "memory": RedisReplicationSpecKubernetesConfigResourcesLimits.from_string("512Mi"),
                    },
                ),
            ),
            redis_config=RedisReplicationSpecRedisConfig(max_memory_percent_of_limit=80),
            storage=RedisReplicationSpecStorage(
                volume_claim_template=RedisReplicationSpecStorageVolumeClaimTemplate(
                    spec=RedisReplicationSpecStorageVolumeClaimTemplateSpec(
                        access_modes=["ReadWriteOnce"],
                        storage_class_name="local-path-ovh",
                        resources=RedisReplicationSpecStorageVolumeClaimTemplateSpecResources(
                            requests={
                                "storage": RedisReplicationSpecStorageVolumeClaimTemplateSpecResourcesRequests.from_string(
                                    "2Gi"
                                )
                            }
                        ),
                    )
                )
            ),
            affinity=RedisReplicationSpecAffinity(
                node_affinity=RedisReplicationSpecAffinityNodeAffinity(
                    required_during_scheduling_ignored_during_execution=RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                        node_selector_terms=[
                            RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms(
                                match_expressions=[
                                    RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions(
                                        key="topology.kubernetes.io/zone", operator="In", values=["hil-ovh"]
                                    )
                                ]
                            )
                        ]
                    ),
                    # Prefer ordinary workers when this workload tolerates control planes.
                    preferred_during_scheduling_ignored_during_execution=[
                        RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecution(
                            weight=100,
                            preference=RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreference(
                                match_expressions=[
                                    RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreferenceMatchExpressions(
                                        key="node-role.kubernetes.io/control-plane", operator="DoesNotExist"
                                    )
                                ]
                            ),
                        )
                    ],
                ),
                pod_anti_affinity=RedisReplicationSpecAffinityPodAntiAffinity(
                    required_during_scheduling_ignored_during_execution=[
                        RedisReplicationSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                            label_selector=RedisReplicationSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecutionLabelSelector(
                                match_labels={"app": _VALKEY}
                            ),
                            topology_key="kubernetes.io/hostname",
                        )
                    ]
                ),
            ),
        ),
    )


def _secret_key_ref(name: str, key: str) -> dict[str, object]:
    return {"secretKeyRef": {"name": name, "key": key}}


def _values() -> dict[str, object]:
    control_plane = "node-role.kubernetes.io/control-plane"
    resources = {"requests": {"cpu": "100m", "memory": "1Gi"}, "limits": {"cpu": "1", "memory": "2Gi"}}
    return {
        "langfuse": {
            # Chart 2.1.0's appVersion trails the current release; pin both web and
            # worker to the same current Langfuse image explicitly.
            "image": {"tag": "4.35.0"},
            "features": {
                # SSO-only: email/password login is disabled below via
                # AUTH_DISABLE_USERNAME_PASSWORD. Signup must stay enabled so the
                # Authentik-gated SSO flow can establish the session on first login;
                # access is bounded by the langfuse application's admins-group policy
                # binding in Authentik (tf/gitops/sso-providers/provider_langfuse.tf).
                "signUpDisabled": False
            },
            "nodeSelector": {"topology.kubernetes.io/zone": _ZONE},
            # Langfuse is stateless at the pod level and uses external storage. Allow
            # control-plane nodes as overflow capacity, while the affinity below keeps
            # ordinary placement on workers.
            "tolerations": [{"key": control_plane, "operator": "Exists", "effect": "NoSchedule"}],
            # Prefer ordinary workers when this workload tolerates control planes.
            "affinity": {
                "nodeAffinity": {
                    "preferredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "weight": 100,
                            "preference": {"matchExpressions": [{"key": control_plane, "operator": "DoesNotExist"}]},
                        }
                    ]
                }
            },
            "nextauth": {
                "url": "https://langfuse.allegedly.works",
                "secret": _secret_key_ref("langfuse-secrets", "nextauth-secret"),
            },
            "salt": _secret_key_ref("langfuse-secrets", "salt"),
            "encryptionKey": _secret_key_ref("langfuse-secrets", "encryption-key"),
            "web": {
                "resources": resources,
                # The image runs initialization before its health endpoints are
                # available; give it time to finish instead of restarting it during
                # startup.
                "livenessProbe": {"initialDelaySeconds": 300},
            },
            "worker": {"resources": resources, "livenessProbe": {"initialDelaySeconds": 300}},
            # Headless initialization — bootstrap org/project/user on first startup.
            # API keys live in langfuse-secrets so LiteLLM can consume the same keys
            # later without a manual copy step.
            "additionalEnv": [
                {"name": "NODE_OPTIONS", "value": "--max-old-space-size=1536"},
                # The shared ClickHouse uses the canonical default logical cluster
                # name, so Langfuse can keep automatic migrations enabled.
                {"name": "CLICKHOUSE_CLUSTER_NAME", "value": "default"},
                # v3 -> v4 migration: dual-write has been validated with fresh LiteLLM
                # chat and Responses traffic. Backfill existing history as-is,
                # including any already-corrupt timestamps.
                # CLEANUP(langfuse-v4-migration): remove these migration overrides once
                # compatible producers, v4 API consumers, evaluators, and exports are
                # verified and historic backfill has completed; that selects v4's
                # events_only/direct defaults and stops legacy writes.
                {"name": "LANGFUSE_MIGRATION_V4_WRITE_MODE", "value": "dual"},
                {"name": "LANGFUSE_MIGRATION_V4_NATIVE_OTEL_BEHAVIOUR", "value": "dual_write"},
                {"name": "LANGFUSE_BACKGROUND_MIGRATION_V4_ENABLE_HISTORIC_BACKFILL", "value": "true"},
                # Authentik SSO (generic OIDC relying party). Client credentials come
                # from the langfuse-oidc-config secret, minted by
                # tf/gitops/sso-providers/provider_langfuse.tf in the authentik
                # namespace and reflected here by emberstack reflector.
                {"name": "AUTH_CUSTOM_NAME", "value": "Authentik"},
                {
                    "name": "AUTH_CUSTOM_ISSUER",
                    # No trailing slash: next-auth builds the discovery URL as
                    # ${AUTH_CUSTOM_ISSUER}/.well-known/openid-configuration, and a
                    # trailing slash yields a double slash that Authentik 301-redirects —
                    # openid-client doesn't follow redirects on discovery (OAuthSignin).
                    "value": "https://auth.allegedly.works/application/o/langfuse",
                },
                {"name": "AUTH_CUSTOM_SCOPE", "value": "openid email profile"},
                {"name": "AUTH_CUSTOM_CLIENT_ID", "valueFrom": _secret_key_ref("langfuse-oidc-config", "client-id")},
                {
                    "name": "AUTH_CUSTOM_CLIENT_SECRET",
                    "valueFrom": _secret_key_ref("langfuse-oidc-config", "client-secret"),
                },
                # Link the SSO identity to the headless-init admin user (same email)
                # so login lands on the existing org/project instead of an empty one.
                {"name": "AUTH_CUSTOM_ALLOW_ACCOUNT_LINKING", "value": "true"},
                # SSO-only: disable email/password login and signup entirely.
                {"name": "AUTH_DISABLE_USERNAME_PASSWORD", "value": "true"},
                {"name": "LANGFUSE_INIT_ORG_ID", "value": "langfuse-default-org"},
                {"name": "LANGFUSE_INIT_ORG_NAME", "value": "Default"},
                {"name": "LANGFUSE_INIT_PROJECT_ID", "value": "langfuse-litellm-project"},
                {"name": "LANGFUSE_INIT_PROJECT_NAME", "value": "litellm"},
                {
                    "name": "LANGFUSE_INIT_PROJECT_PUBLIC_KEY",
                    "valueFrom": _secret_key_ref("langfuse-secrets", "LANGFUSE_INIT_PROJECT_PUBLIC_KEY"),
                },
                {
                    "name": "LANGFUSE_INIT_PROJECT_SECRET_KEY",
                    "valueFrom": _secret_key_ref("langfuse-secrets", "LANGFUSE_INIT_PROJECT_SECRET_KEY"),
                },
                # Email matches the Authentik identity (agentydragon@gmail.com) so the
                # SSO account links to this org owner. Headless init adds this user as
                # an owner of langfuse-default-org on startup.
                {"name": "LANGFUSE_INIT_USER_EMAIL", "value": "agentydragon@gmail.com"},
                {"name": "LANGFUSE_INIT_USER_NAME", "value": "Rai"},
                {
                    "name": "LANGFUSE_INIT_USER_PASSWORD",
                    "valueFrom": _secret_key_ref("langfuse-secrets", "LANGFUSE_INIT_USER_PASSWORD"),
                },
            ],
        },
        "postgresql": {
            "deploy": False,
            "host": "langfuse-db-rw",
            "auth": {
                "username": "langfuse",
                "database": "langfuse",
                "existingSecret": "langfuse-db-app",
                "secretKeys": {"userPasswordKey": "password"},
            },
        },
        # ClickHouse is managed centrally in the clickhouse namespace.
        "clickhouse": {
            "deploy": False,
            "host": "clickhouse.clickhouse.svc.cluster.local",
            "httpPort": 8123,
            "nativePort": 9000,
            "database": "langfuse",
            "auth": {
                "username": "langfuse",
                "existingSecret": "clickhouse-langfuse-credentials",
                "existingSecretKey": "password",
            },
            "migration": {
                "url": "clickhouse://clickhouse.clickhouse.svc.cluster.local:9000",
                "ssl": False,
                "autoMigrate": True,
            },
            "clusterEnabled": True,
        },
        "redis": {
            "deploy": False,
            "host": f"{_VALKEY}-master.{_NAMESPACE}.svc.cluster.local",
            "port": 6379,
            # The Valkey runs without auth. cdk8s drops None, so the chart's default
            # username cannot be nulled; an empty one renders the same URL (the chart
            # adds `user@` only for a non-empty username). Dropping the key instead
            # would bring back the default and add `default@`.
            "auth": {"username": ""},
        },
        "s3": {
            "deploy": False,
            "storageProvider": "s3",
            "bucket": _NAME,
            "region": "auto",
            "endpoint": "http://seaweedfs-s3.seaweedfs.svc:8333",
            "forcePathStyle": True,
            "accessKeyId": _secret_key_ref(_S3_CREDENTIALS_SECRET, "s3-access-key-id"),
            "secretAccessKey": _secret_key_ref(_S3_CREDENTIALS_SECRET, "s3-secret-access-key"),
            "eventUpload": {"prefix": "events/"},
            "batchExport": {"prefix": "exports/"},
            "mediaUpload": {"prefix": "media/"},
        },
        # Ingress disabled — using Gateway API HTTPRoute
        "ingress": {"enabled": False},
    }


def _helm_release(scope: Construct) -> None:
    repository = HelmRepository(
        scope,
        "helm-repository",
        metadata=metadata(_NAME, _NAMESPACE),
        spec=HelmRepositorySpec(interval="24h", url="https://langfuse.github.io/langfuse-k8s"),
    )
    HelmRelease(
        scope,
        "helm-release",
        metadata=metadata(_NAME, _NAMESPACE),
        spec=HelmReleaseSpec(
            interval="15m",
            timeout="20m",
            install=HelmReleaseSpecInstall(
                strategy=HelmReleaseSpecInstallStrategy(name=HelmReleaseSpecInstallStrategyName.RETRY_ON_FAILURE)
            ),
            upgrade=HelmReleaseSpecUpgrade(
                strategy=HelmReleaseSpecUpgradeStrategy(name=HelmReleaseSpecUpgradeStrategyName.RETRY_ON_FAILURE)
            ),
            chart=HelmReleaseSpecChart(
                spec=HelmReleaseSpecChartSpec(
                    chart=_NAME,
                    version="2.1.0",
                    source_ref=HelmReleaseSpecChartSpecSourceRef(
                        kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY,
                        name=repository.name,
                        namespace=repository.metadata.namespace,
                    ),
                )
            ),
            values=_values(),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    _namespace(chart)
    _database(chart)
    _storage(chart)
    https_route(
        chart,
        "route",
        metadata=metadata(_NAME, _NAMESPACE),
        hostname="langfuse.allegedly.works",
        backend="langfuse-web",
        port=3000,
        hsts=False,
        listener=None,
    )
    _log_reader(chart)
    _valkey(chart)
    _helm_release(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[f"{_NAME}.k8s.yaml", "langfuse-secrets.sops.yaml"]),
    )


def langfuse(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    cnpg: Kustomization,
    valkey: Kustomization,
    seaweedfs_operator: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        _NAME,
        artifact,
        suspend=False,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        decryption=SOPS_DECRYPTION,
        timeout="20m",
        depends_on=flux_kustomization_depends_on_many(cnpg, valkey, seaweedfs_operator),
    )
