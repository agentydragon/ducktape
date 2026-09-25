"""The haku-openclaw-spike state backup: Restic snapshots through VolSync into the app's
SeaweedFS bucket (`haku_openclaw_spike_config._backup_bucket`). The SOPS-encrypted Restic
password beside the output stays hand-written.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
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
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts
from volsync_replicationsource_crds.backube.volsync import (
    ReplicationSource,
    ReplicationSourceSpec,
    ReplicationSourceSpecRestic,
    ReplicationSourceSpecResticCacheCapacity,
    ReplicationSourceSpecResticCopyMethod,
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
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.providers.external_secrets.external_secret import add_external_secret, remote_data, secret_store

NAME = "haku-openclaw-spike-backup"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/haku-openclaw-spike/backup"
_NAMESPACE = "haku-openclaw-spike"
_MOVER_LABELS = {"app.kubernetes.io/name": "haku-openclaw-spike-volsync"}
_REPOSITORY_SECRET_NAME = "haku-openclaw-spike-volsync-restic"
# The app's Bucket.
_BUCKET_NAME = "haku-openclaw-spike-backups"
_SECRET_STORE_NAME = "haku-openclaw-spike-volsync-s3"
_REPOSITORY_READER = "haku-openclaw-spike-volsync-repository-reader"
# Written by the app's S3Credentials.
_S3_CREDENTIALS_SECRET_NAME = "haku-openclaw-spike-volsync-s3-credentials"
# SOPS-encrypted in repository.sops.yaml.
_RESTIC_PASSWORD_SECRET_NAME = "haku-openclaw-spike-volsync-restic-password"


def _network_policy(scope: Construct) -> None:
    k8s.KubeNetworkPolicy(
        scope,
        "volsync-egress",
        metadata=k8s.ObjectMeta(name="haku-openclaw-spike-volsync-egress", namespace=_NAMESPACE),
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
                                match_labels={"kubernetes.io/metadata.name": "seaweedfs"}
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
                resource_names=[_S3_CREDENTIALS_SECRET_NAME, _RESTIC_PASSWORD_SECRET_NAME],
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


def _repository(scope: Construct) -> None:
    """The combined repository Secret VolSync requires, rendered by ESO from the S3 credentials
    and the Restic password."""
    add_external_secret(
        scope,
        "repository",
        name=_REPOSITORY_SECRET_NAME,
        namespace=_NAMESPACE,
        refresh="1h",
        store=secret_store(_SECRET_STORE_NAME),
        data=[
            remote_data(_S3_CREDENTIALS_SECRET_NAME, "AWS_ACCESS_KEY_ID"),
            remote_data(_S3_CREDENTIALS_SECRET_NAME, "AWS_SECRET_ACCESS_KEY"),
            remote_data(_RESTIC_PASSWORD_SECRET_NAME, "RESTIC_PASSWORD"),
        ],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        template=ExternalSecretSpecTargetTemplate(
            type="Opaque",
            # Preserve the data fetched above alongside the static Restic endpoint.
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
    )


def _replication_source(scope: Construct) -> None:
    """Encrypted, deduplicated Restic snapshots of the OpenClaw spike state. Direct copies are
    crash-consistent.

    The source is the optiplex home worker PVC (state-v2, local-path-home-ssd). The Direct mover
    mounts that already-bound PVC, so the PV's own node affinity forces the mover (and its
    WaitForFirstConsumer cache) onto optiplex -- no explicit moverAffinity needed. (The restore
    side did pin its mover: its destination PVC was unbound, so the mover's placement is what
    chose the node.)
    """
    ReplicationSource(
        scope,
        "state-restic",
        metadata=metadata("haku-openclaw-spike-state-restic", _NAMESPACE),
        spec=ReplicationSourceSpec(
            source_pvc="haku-openclaw-spike-state-v2",
            # 03:23 UTC, away from interactive use and staggered from public-coder's 09:17.
            trigger=ReplicationSourceSpecTrigger(schedule="23 03 * * *"),
            restic=ReplicationSourceSpecRestic(
                repository=_REPOSITORY_SECRET_NAME,
                copy_method=ReplicationSourceSpecResticCopyMethod.DIRECT,
                prune_interval_days=7,
                retain=ReplicationSourceSpecResticRetain(daily=7, weekly=4, monthly=6),
                cache_storage_class_name="local-path-home-ssd",
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
            ),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
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


def haku_openclaw_spike_backup(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    external_secrets_operator: Kustomization,
    volsync: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        timeout="5m",
        decryption=SOPS_DECRYPTION,
        depends_on=flux_kustomization_depends_on_many(
            # Backup/S3 wiring must converge even when the OpenClaw Deployment is down.
            # The Bucket and S3Credentials remain app-owned, but their readiness is
            # retried by the ExternalSecret rather than coupling this Kustomization to
            # the app Deployment health check.
            external_secrets_operator,
            volsync,
        ),
        description=(
            "Restic/VolSync backup of the Haku OpenClaw spike state to its "
            "dedicated private SeaweedFS S3 bucket, plus the one-shot restore "
            "into the optiplex worker PVC that migrates the state off the control "
            "plane."
        ),
    )
