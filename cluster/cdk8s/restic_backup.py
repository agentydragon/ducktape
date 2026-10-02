"""`ResticBackup`: a VolSync Restic backup of one PVC into a SeaweedFS bucket."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, Size
from cdk8s_plus_34 import Cpu, k8s
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateMergePolicy,
    ExternalSecretSpecTargetTemplateTemplateFrom,
)
from volsync_replicationsource_crds.backube.volsync import (
    ReplicationSourceSpecRestic,
    ReplicationSourceSpecResticCacheCapacity,
    ReplicationSourceSpecResticCopyMethod,
    ReplicationSourceSpecResticMoverAffinity,
    ReplicationSourceSpecResticMoverResources,
    ReplicationSourceSpecResticMoverResourcesLimits,
    ReplicationSourceSpecResticMoverResourcesRequests,
    ReplicationSourceSpecResticMoverSecurityContext,
    ReplicationSourceSpecResticMoverSecurityContextSeccompProfile,
    ReplicationSourceSpecResticRetain,
    ReplicationSourceSpecTrigger,
)

from cluster.cdk8s.external_secrets.kubernetes_store import secret_store
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, SecretStoreRef, remote_data
from cluster.cdk8s.providers.volsync.replication_source import ReplicationSource


class ResticBackup(Construct):
    """Encrypted, deduplicated Restic snapshots of PVC `source_pvc` on `schedule`, taken by
    ReplicationSource `name` into SeaweedFS bucket `bucket`. Direct copies are
    crash-consistent.

    The bucket, and the S3Credentials writing its key pair into Secret `s3_credentials`, stay
    the caller's. That Secret and the password in Secret `<namespace>-volsync-restic-password`
    (a SOPS sibling) are the only Secrets the SecretStore may read; ESO renders them into
    Secret `repository`, which VolSync reads.

    Our policy: keep 7 daily, 4 weekly and 6 monthly snapshots and prune weekly; the mover runs
    as uid and gid 1000 and reaches only DNS and SeaweedFS's S3 port. `mover_affinity=None`
    sets none: the Direct mover mounts the source PVC, so a node-local volume already places it.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        namespace: str,
        name: str,
        repository: str,
        source_pvc: str,
        bucket: str,
        s3_credentials: str,
        schedule: str,
        cache_storage_class_name: str,
        mover_affinity: ReplicationSourceSpecResticMoverAffinity | None = None,
    ) -> None:
        super().__init__(scope, id)
        mover_labels = {"app.kubernetes.io/name": f"{namespace}-volsync"}
        restic_password = f"{namespace}-volsync-restic-password"
        self.network_policy = k8s.KubeNetworkPolicy(
            self,
            "volsync-egress",
            metadata=k8s.ObjectMeta(name="volsync-egress", namespace=namespace),
            spec=k8s.NetworkPolicySpec(
                pod_selector=k8s.LabelSelector(match_labels=mover_labels),
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
        reader = k8s.ObjectMeta(name="volsync-repository-reader", namespace=namespace)
        self.service_account = k8s.KubeServiceAccount(self, "repository-reader-sa", metadata=reader)
        self.role = k8s.KubeRole(
            self,
            "repository-reader-role",
            metadata=reader,
            rules=[
                k8s.PolicyRule(
                    api_groups=[""],
                    resources=["secrets"],
                    resource_names=[s3_credentials, restic_password],
                    verbs=["get"],
                )
            ],
        )
        self.role_binding = k8s.KubeRoleBinding(
            self,
            "repository-reader-rolebinding",
            metadata=reader,
            role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=self.role.name),
            subjects=[k8s.Subject(kind="ServiceAccount", name=self.service_account.name, namespace=namespace)],
        )
        self.secret_store = secret_store(
            self,
            "repository-store",
            metadata=ApiObjectMetadata(name=f"{namespace}-volsync-s3", namespace=namespace),
            reader=self.service_account,
        )
        self.external_secret = ExternalSecret(
            self,
            "repository",
            metadata=ApiObjectMetadata(name=repository, namespace=namespace),
            refresh_interval="1h",
            secret_store_ref=SecretStoreRef.namespaced(self.secret_store.name),
            data=[
                remote_data(s3_credentials, "AWS_ACCESS_KEY_ID"),
                remote_data(s3_credentials, "AWS_SECRET_ACCESS_KEY"),
                remote_data(restic_password, "RESTIC_PASSWORD"),
            ],
            creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
            template=ExternalSecretSpecTargetTemplate(
                type="Opaque",
                # Preserve the data fetched above alongside the static Restic endpoint.
                merge_policy=ExternalSecretSpecTargetTemplateMergePolicy.MERGE,
                template_from=[
                    ExternalSecretSpecTargetTemplateTemplateFrom(
                        literal=(
                            f"RESTIC_REPOSITORY: s3:http://seaweedfs-s3.seaweedfs.svc:8333/{bucket}\n"
                            "AWS_DEFAULT_REGION: us-east-1\n"
                        )
                    )
                ],
            ),
        )
        self.replication_source = ReplicationSource(
            self,
            "state-restic",
            metadata=ApiObjectMetadata(name=name, namespace=namespace),
            source_pvc=source_pvc,
            trigger=ReplicationSourceSpecTrigger(schedule=schedule),
            mover=ReplicationSourceSpecRestic(
                repository=repository,
                copy_method=ReplicationSourceSpecResticCopyMethod.DIRECT,
                prune_interval_days=7,
                retain=ReplicationSourceSpecResticRetain(daily=7, weekly=4, monthly=6),
                cache_storage_class_name=cache_storage_class_name,
                cache_access_modes=["ReadWriteOnce"],
                cache_capacity=ReplicationSourceSpecResticCacheCapacity.from_string(Size.gibibytes(1).as_string()),
                mover_pod_labels=mover_labels,
                mover_resources=ReplicationSourceSpecResticMoverResources(
                    requests={
                        "cpu": ReplicationSourceSpecResticMoverResourcesRequests.from_string(Cpu.millis(250).amount),
                        "memory": ReplicationSourceSpecResticMoverResourcesRequests.from_string(
                            Size.mebibytes(512).as_string()
                        ),
                    },
                    limits={
                        "cpu": ReplicationSourceSpecResticMoverResourcesLimits.from_string(Cpu.units(1).amount),
                        "memory": ReplicationSourceSpecResticMoverResourcesLimits.from_string(
                            Size.gibibytes(1).as_string()
                        ),
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
                mover_affinity=mover_affinity,
            ),
        )
