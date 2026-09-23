"""The public-coder-agent state backup: Restic snapshots through VolSync into its own
SeaweedFS bucket. The SOPS-encrypted Restic password beside the output stays hand-written.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecData,
    ExternalSecretSpecDataRemoteRef,
    ExternalSecretSpecSecretStoreRef,
    ExternalSecretSpecSecretStoreRefKind,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateMergePolicy,
    ExternalSecretSpecTargetTemplateTemplateFrom,
)
from external_secrets_secretstore_crds.io.external_secrets import (
    SecretStore,
    SecretStoreSpec,
    SecretStoreSpecProvider,
    SecretStoreSpecProviderKubernetes,
    SecretStoreSpecProviderKubernetesAuth,
    SecretStoreSpecProviderKubernetesAuthServiceAccount,
    SecretStoreSpecProviderKubernetesServer,
    SecretStoreSpecProviderKubernetesServerCaProvider,
    SecretStoreSpecProviderKubernetesServerCaProviderType,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
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
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts
from volsync_replicationsource_crds.backube.volsync import (
    ReplicationSource,
    ReplicationSourceSpec,
    ReplicationSourceSpecRestic,
    ReplicationSourceSpecResticCacheCapacity,
    ReplicationSourceSpecResticCopyMethod,
    ReplicationSourceSpecResticMoverAffinity,
    ReplicationSourceSpecResticMoverAffinityNodeAffinity,
    ReplicationSourceSpecResticMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution,
    ReplicationSourceSpecResticMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms,
    ReplicationSourceSpecResticMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions,
    ReplicationSourceSpecResticMoverResources,
    ReplicationSourceSpecResticMoverResourcesLimits,
    ReplicationSourceSpecResticMoverResourcesRequests,
    ReplicationSourceSpecResticMoverSecurityContext,
    ReplicationSourceSpecResticMoverSecurityContextSeccompProfile,
    ReplicationSourceSpecResticRetain,
    ReplicationSourceSpecTrigger,
)

from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.metadata import metadata

NAME = "public-coder-agent-backup"
OUTPUT_DIR = "cluster/k8s/agents/public-coder-agent/backup"
_NAMESPACE = "public-coder-agent"
_BUCKET_NAME = "public-coder-agent-backups"
_SEAWEEDFS = "seaweedfs"
_MOVER_LABELS = {"app.kubernetes.io/name": "public-coder-agent-volsync"}
_REPOSITORY_SECRET_NAME = "public-coder-agent-state-v2-restic"
_S3_CREDENTIALS_SECRET_NAME = "public-coder-agent-seaweedfs-credentials"
_SECRET_STORE_NAME = "public-coder-agent-volsync-s3"
_REPOSITORY_READER = "public-coder-agent-volsync-repository-reader"
# SOPS-encrypted in repository.sops.yaml.
_RESTIC_PASSWORD_SECRET_NAME = "public-coder-agent-volsync-restic-password"


def _bucket(scope: Construct) -> None:
    Bucket(
        scope,
        "bucket",
        metadata=metadata(
            _BUCKET_NAME,
            _NAMESPACE,
            annotations={"description": "Public Coder's tenant-local SeaweedFS backup bucket."},
        ),
        spec=BucketSpec(
            name=_BUCKET_NAME,
            adopt_existing=True,
            cluster_ref=BucketSpecClusterRef(name=_SEAWEEDFS, namespace=_SEAWEEDFS),
            reclaim_policy=BucketSpecReclaimPolicy.RETAIN,
            access=[
                BucketSpecAccess(
                    user=_BUCKET_NAME,
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
        "credentials",
        metadata=metadata(
            _BUCKET_NAME,
            _NAMESPACE,
            annotations={"description": "Public Coder's tenant-local SeaweedFS backup credentials."},
        ),
        spec=S3CredentialsSpec(
            seaweed_ref=S3CredentialsSpecSeaweedRef(name=_SEAWEEDFS, namespace=_SEAWEEDFS),
            identity_ref=S3CredentialsSpecIdentityRef(name=_BUCKET_NAME),
            secret_ref=S3CredentialsSpecSecretRef(
                name=_S3_CREDENTIALS_SECRET_NAME,
                access_key_field="AWS_ACCESS_KEY_ID",
                secret_key_field="AWS_SECRET_ACCESS_KEY",
            ),
            reclaim_policy=S3CredentialsSpecReclaimPolicy.RETAIN,
        ),
    )
    # Permit only Public Coder's tenant-local Bucket and S3Credentials to reference the SeaweedFS
    # cluster in its namespace.
    group = "seaweed.seaweedfs.com"
    ResourceReferenceGrant(
        scope,
        "reference-grant",
        metadata=metadata(_BUCKET_NAME, _SEAWEEDFS),
        spec=ResourceReferenceGrantSpec(
            from_=[
                ResourceReferenceGrantSpecFrom(group=group, kind="Bucket", namespace=_NAMESPACE),
                ResourceReferenceGrantSpecFrom(group=group, kind="S3Credentials", namespace=_NAMESPACE),
            ],
            to=[ResourceReferenceGrantSpecTo(group=group, kind="Seaweed", name=_SEAWEEDFS)],
        ),
    )


def _network_policy(scope: Construct) -> None:
    k8s.KubeNetworkPolicy(
        scope,
        "volsync-egress",
        metadata=k8s.ObjectMeta(name="public-coder-agent-volsync-egress", namespace=_NAMESPACE),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels=_MOVER_LABELS),
            policy_types=["Egress"],
            egress=[
                k8s.NetworkPolicyEgressRule(
                    to=[
                        k8s.NetworkPolicyPeer(
                            namespace_selector=k8s.LabelSelector(
                                match_labels={"kubernetes.io/metadata.name": "kube-system"}
                            ),
                            pod_selector=k8s.LabelSelector(match_labels={"k8s-app": "kube-dns"}),
                        )
                    ],
                    ports=[
                        k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(53), protocol="UDP"),
                        k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(53), protocol="TCP"),
                    ],
                ),
                k8s.NetworkPolicyEgressRule(
                    to=[
                        k8s.NetworkPolicyPeer(
                            namespace_selector=k8s.LabelSelector(
                                match_labels={"kubernetes.io/metadata.name": _SEAWEEDFS}
                            ),
                            pod_selector=k8s.LabelSelector(
                                match_labels={
                                    "app.kubernetes.io/component": "s3",
                                    "app.kubernetes.io/instance": "seaweedfs",
                                    "app.kubernetes.io/managed-by": "seaweedfs-operator",
                                    "app.kubernetes.io/name": "seaweedfs",
                                }
                            ),
                        )
                    ],
                    ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(8333), protocol="TCP")],
                ),
            ],
        ),
    )


def _repository_store(scope: Construct) -> None:
    """S3Credentials and the SOPS-managed Restic password create source Secrets in this
    namespace. ESO narrows its access to exactly those Secrets and renders the combined
    repository Secret VolSync requires."""
    k8s.KubeServiceAccount(
        scope, "repository-reader-sa", metadata=k8s.ObjectMeta(name=_REPOSITORY_READER, namespace=_NAMESPACE)
    )
    k8s.KubeRole(
        scope,
        "repository-reader-role",
        metadata=k8s.ObjectMeta(name=_REPOSITORY_READER, namespace=_NAMESPACE),
        rules=[
            k8s.PolicyRule(
                api_groups=[""],
                resources=["secrets"],
                resource_names=[
                    "public-coder-agent-volsync-s3",
                    _S3_CREDENTIALS_SECRET_NAME,
                    _RESTIC_PASSWORD_SECRET_NAME,
                ],
                verbs=["get"],
            )
        ],
    )
    k8s.KubeRoleBinding(
        scope,
        "repository-reader-rolebinding",
        metadata=k8s.ObjectMeta(name=_REPOSITORY_READER, namespace=_NAMESPACE),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=_REPOSITORY_READER),
        subjects=[k8s.Subject(kind="ServiceAccount", name=_REPOSITORY_READER, namespace=_NAMESPACE)],
    )
    SecretStore(
        scope,
        "repository-store",
        metadata=metadata(_SECRET_STORE_NAME, _NAMESPACE),
        spec=SecretStoreSpec(
            provider=SecretStoreSpecProvider(
                kubernetes=SecretStoreSpecProviderKubernetes(
                    server=SecretStoreSpecProviderKubernetesServer(
                        ca_provider=SecretStoreSpecProviderKubernetesServerCaProvider(
                            type=SecretStoreSpecProviderKubernetesServerCaProviderType.CONFIG_MAP,
                            name="kube-root-ca.crt",
                            key="ca.crt",
                        )
                    ),
                    auth=SecretStoreSpecProviderKubernetesAuth(
                        service_account=SecretStoreSpecProviderKubernetesAuthServiceAccount(
                            name=_REPOSITORY_READER, namespace=_NAMESPACE
                        )
                    ),
                    remote_namespace=_NAMESPACE,
                )
            )
        ),
    )


def _remote_ref(secret_key: str, secret_name: str) -> ExternalSecretSpecData:
    return ExternalSecretSpecData(
        secret_key=secret_key, remote_ref=ExternalSecretSpecDataRemoteRef(key=secret_name, property=secret_key)
    )


def _repository(scope: Construct) -> None:
    """The combined repository Secret VolSync requires, rendered by ESO from the S3 credentials
    and the Restic password."""
    ExternalSecret(
        scope,
        "repository",
        metadata=metadata(_REPOSITORY_SECRET_NAME, _NAMESPACE),
        spec=ExternalSecretSpec(
            refresh_interval="1h",
            secret_store_ref=ExternalSecretSpecSecretStoreRef(
                kind=ExternalSecretSpecSecretStoreRefKind.SECRET_STORE, name=_SECRET_STORE_NAME
            ),
            target=ExternalSecretSpecTarget(
                name=_REPOSITORY_SECRET_NAME,
                creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
                template=ExternalSecretSpecTargetTemplate(
                    type="Opaque",
                    # Preserve the data fetched below alongside the static Restic endpoint.
                    merge_policy=ExternalSecretSpecTargetTemplateMergePolicy.MERGE,
                    template_from=[
                        ExternalSecretSpecTargetTemplateTemplateFrom(
                            literal=(
                                f"RESTIC_REPOSITORY: s3:http://seaweedfs-s3.seaweedfs.svc:8333/{_BUCKET_NAME}\n"
                                "AWS_DEFAULT_REGION: us-east-1\n"
                            )
                        )
                    ],
                ),
            ),
            data=[
                _remote_ref("AWS_ACCESS_KEY_ID", _S3_CREDENTIALS_SECRET_NAME),
                _remote_ref("AWS_SECRET_ACCESS_KEY", _S3_CREDENTIALS_SECRET_NAME),
                _remote_ref("RESTIC_PASSWORD", _RESTIC_PASSWORD_SECRET_NAME),
            ],
        ),
    )


def _replication_source(scope: Construct) -> None:
    """Encrypted, deduplicated snapshots of the worker-local OpenClaw state. Direct copies are
    crash-consistent: run and retain a restore drill before treating them as a replacement for
    the old PVC/rescue archive."""
    ReplicationSource(
        scope,
        "state-restic",
        metadata=metadata(_REPOSITORY_SECRET_NAME, _NAMESPACE),
        spec=ReplicationSourceSpec(
            source_pvc="public-coder-agent-state-v2",
            # 09:17 UTC is 02:17 PDT / 01:17 PST; run away from normal interactive use.
            trigger=ReplicationSourceSpecTrigger(schedule="17 09 * * *"),
            restic=ReplicationSourceSpecRestic(
                repository=_REPOSITORY_SECRET_NAME,
                copy_method=ReplicationSourceSpecResticCopyMethod.DIRECT,
                prune_interval_days=7,
                retain=ReplicationSourceSpecResticRetain(daily=7, weekly=4, monthly=6),
                cache_storage_class_name="local-path-ovh-hdd",
                cache_access_modes=["ReadWriteOnce"],
                cache_capacity=ReplicationSourceSpecResticCacheCapacity.from_string("1Gi"),
                mover_pod_labels=_MOVER_LABELS,
                mover_resources=ReplicationSourceSpecResticMoverResources(
                    requests={
                        "cpu": ReplicationSourceSpecResticMoverResourcesRequests.from_string("250m"),
                        "memory": ReplicationSourceSpecResticMoverResourcesRequests.from_string("512Mi"),
                    },
                    limits={
                        "cpu": ReplicationSourceSpecResticMoverResourcesLimits.from_string("1"),
                        "memory": ReplicationSourceSpecResticMoverResourcesLimits.from_string("1Gi"),
                    },
                ),
                mover_security_context=ReplicationSourceSpecResticMoverSecurityContext(
                    run_as_non_root=True,
                    run_as_user=1000,
                    run_as_group=1000,
                    fs_group=1000,
                    seccomp_profile=ReplicationSourceSpecResticMoverSecurityContextSeccompProfile(
                        type="RuntimeDefault"
                    ),
                ),
                mover_affinity=ReplicationSourceSpecResticMoverAffinity(
                    node_affinity=ReplicationSourceSpecResticMoverAffinityNodeAffinity(
                        required_during_scheduling_ignored_during_execution=ReplicationSourceSpecResticMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                            node_selector_terms=[
                                ReplicationSourceSpecResticMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms(
                                    match_expressions=[
                                        ReplicationSourceSpecResticMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions(
                                            key="kubernetes.io/hostname", operator="In", values=["ovh-ns102453"]
                                        )
                                    ]
                                )
                            ]
                        )
                    )
                ),
            ),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    _bucket(chart)
    _network_policy(chart)
    _repository_store(chart)
    _repository(chart)
    _replication_source(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[f"{NAME}.k8s.yaml", "repository.sops.yaml"]),
    )


def public_coder_agent_backup(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    seaweedfs_public_coder_agent_backups_bucket: Kustomization,
    external_secrets_config: Kustomization,
    volsync: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        timeout="5m",
        decryption=SOPS_DECRYPTION,
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="seaweed.seaweedfs.com/v1",
                kind="Bucket",
                name="public-coder-agent-backups",
                namespace="public-coder-agent",
            ),
            KustomizationSpecHealthChecks(
                api_version="seaweed.seaweedfs.com/v1",
                kind="S3Credentials",
                name="public-coder-agent-backups",
                namespace="public-coder-agent",
            ),
            KustomizationSpecHealthChecks(
                api_version="external-secrets.io/v1",
                kind="ExternalSecret",
                name="public-coder-agent-state-v2-restic",
                namespace="public-coder-agent",
            ),
        ],
        depends_on=flux_kustomization_depends_on_many(
            seaweedfs_public_coder_agent_backups_bucket, external_secrets_config, volsync
        ),
        description=(
            "Restic/VolSync backup of Public Coder's worker-local OpenClaw state "
            "to its dedicated private SeaweedFS S3 bucket."
        ),
    )
