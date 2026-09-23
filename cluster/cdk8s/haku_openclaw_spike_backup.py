"""The haku-openclaw-spike state backup: Restic snapshots through VolSync into the app's
SeaweedFS bucket (`haku_openclaw_spike_config._backup_bucket`).

`repository-secret-store.yaml` (the SecretStore and the identity it reads with) stays
hand-written beside the generated file: no namespaced `SecretStore` CRD binding exists.
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

from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "haku-openclaw-spike-backup"
OUTPUT_DIR = "cluster/k8s/agents/haku-openclaw-spike/backup"
_NAMESPACE = "haku-openclaw-spike"
_MOVER_LABELS = {"app.kubernetes.io/name": "haku-openclaw-spike-volsync"}
_REPOSITORY_SECRET_NAME = "haku-openclaw-spike-volsync-restic"
# The app's Bucket.
_BUCKET_NAME = "haku-openclaw-spike-backups"
# Hand-written in repository-secret-store.yaml.
_SECRET_STORE_NAME = "haku-openclaw-spike-volsync-s3"
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
    _repository(chart)
    _replication_source(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
