"""Grocy itself, one directory per household (`<household>/app`, the household's Flux
Kustomization directory): its Namespace, the Deployment, Service, settings overrides and
ingress NetworkPolicy, the config PVC, and the VolSync backup, migration and verification
objects around it. The directory's `kustomization.yaml` also includes the household's MCP
server (`../mcp`, cluster/cdk8s/grocy/mcp.py).
"""

from __future__ import annotations

import posixpath
from functools import partial
from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from volsync_replicationdestination_crds.backube.volsync import (
    ReplicationDestinationSpecRsyncTls,
    ReplicationDestinationSpecRsyncTlsCopyMethod,
    ReplicationDestinationSpecRsyncTlsMoverAffinity,
    ReplicationDestinationSpecRsyncTlsMoverAffinityNodeAffinity,
    ReplicationDestinationSpecRsyncTlsMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution,
    ReplicationDestinationSpecRsyncTlsMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms,
    ReplicationDestinationSpecRsyncTlsMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions,
    ReplicationDestinationSpecRsyncTlsMoverSecurityContext,
    ReplicationDestinationSpecRsyncTlsMoverSecurityContextSeccompProfile,
    ReplicationDestinationSpecTrigger,
)
from volsync_replicationsource_crds.backube.volsync import (
    ReplicationSourceSpecRsyncTls,
    ReplicationSourceSpecRsyncTlsCopyMethod,
    ReplicationSourceSpecRsyncTlsMoverAffinity,
    ReplicationSourceSpecRsyncTlsMoverAffinityNodeAffinity,
    ReplicationSourceSpecRsyncTlsMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution,
    ReplicationSourceSpecRsyncTlsMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms,
    ReplicationSourceSpecRsyncTlsMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions,
    ReplicationSourceSpecRsyncTlsMoverSecurityContext,
    ReplicationSourceSpecRsyncTlsMoverSecurityContextSeccompProfile,
    ReplicationSourceSpecTrigger,
)

from cluster.cdk8s import cilium, namespaces, node_scheduling
from cluster.cdk8s.authentik import app as authentik  # `app` is the cdk8s App parameter here
from cluster.cdk8s.flux import kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.volsync.replication_destination import ReplicationDestination
from cluster.cdk8s.providers.volsync.replication_source import ReplicationSource
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

_NAME = "grocy"
_LABELS = {"app.kubernetes.io/name": _NAME}
_IMAGE = "lscr.io/linuxserver/grocy:v4.6.0-ls318"
_CONFIG_CLAIM = "grocy-config-ovh"
_BACKUP = "grocy-config-ovh-backup"
_HTTP = Port(name="http", number=80)
# grocy/mcp.py and grocy/user_perms.py render one of theirs per household too.
HOUSEHOLDS = ("sf", "vallejo")
# Every grocy directory but the hand-written image-pins Components: `<household>/{app,mcp,user-perms}`
# and `user-perms-base`.
ROOT = f"{GENERATED_ROOT}/grocy"
_BACKUP_SCHEDULE = "23 */6 * * *"


def output_dir(household: str) -> str:
    return f"{ROOT}/{household}/app"


def service(household: str) -> ServiceRef:
    return ServiceRef(name=_NAME, port=_HTTP, pods=Pods(namespace=f"grocy-{household}", labels=tuple(_LABELS.items())))


def hostname(household: str) -> str:
    """The household's public host, served through Authentik's proxy outpost
    (cluster/cdk8s/authentik/proxy_routes.py)."""
    return f"grocy-{household}.allegedly.works"


def _from_namespace_pod(namespace: str, pod_labels: dict[str, str]) -> k8s.NetworkPolicyIngressRule:
    return k8s.NetworkPolicyIngressRule(
        from_=[
            k8s.NetworkPolicyPeer(
                namespace_selector=k8s.LabelSelector(match_labels={"kubernetes.io/metadata.name": namespace}),
                pod_selector=k8s.LabelSelector(match_labels=pod_labels),
            )
        ],
        ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(_HTTP.number), protocol="TCP")],
    )


def _server(chart: Chart, namespace: str) -> None:
    login_probe = k8s.HttpGetAction(path="/login", port=k8s.IntOrString.from_number(_HTTP.number))
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=namespace, labels=_LABELS),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=_LABELS),
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    node_selector={"topology.kubernetes.io/region": "hil", **node_scheduling.HIL_OVH_NODE_SELECTOR},
                    containers=[
                        k8s.Container(
                            name=_NAME,
                            image=_IMAGE,
                            ports=[_HTTP.k8s_container_port()],
                            env=[
                                k8s.EnvVar(name="PUID", value="1000"),
                                k8s.EnvVar(name="PGID", value="1000"),
                                k8s.EnvVar(name="TZ", value="UTC"),
                            ],
                            volume_mounts=[
                                k8s.VolumeMount(name="config", mount_path="/config"),
                                k8s.VolumeMount(name="settingoverrides", mount_path="/config/data/settingoverrides"),
                            ],
                            # Trigger Grocy's lazy schema migration at startup so a fresh DB is
                            # migrated before anything downstream (the MCP, logins, the user-perms
                            # Terraform) hits it. `/` is auth-exempt and runs MigrateDatabase()
                            # then 302s. Best-effort: bounded retry until the server is up, never
                            # fails the container — Grocy's normal lazy migration still covers the
                            # first real request if the server is slow to come up here.
                            lifecycle=k8s.Lifecycle(
                                post_start=k8s.LifecycleHandler(
                                    exec=k8s.ExecAction(
                                        command=[
                                            "sh",
                                            "-c",
                                            "i=0; while [ $i -lt 60 ]; do curl -fsS -o /dev/null"
                                            " http://localhost:80/ && exit 0; sleep 2; i=$((i+1)); done",
                                        ]
                                    )
                                )
                            ),
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("50m"),
                                    "memory": k8s.Quantity.from_string("128Mi"),
                                },
                                limits={
                                    "cpu": k8s.Quantity.from_string("500m"),
                                    "memory": k8s.Quantity.from_string("512Mi"),
                                },
                            ),
                            liveness_probe=k8s.Probe(http_get=login_probe, initial_delay_seconds=30, period_seconds=10),
                            readiness_probe=k8s.Probe(http_get=login_probe, initial_delay_seconds=10, period_seconds=5),
                        )
                    ],
                    volumes=[
                        k8s.Volume(
                            name="config",
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=_CONFIG_CLAIM),
                        ),
                        k8s.Volume(
                            name="settingoverrides", config_map=k8s.ConfigMapVolumeSource(name="grocy-settingoverrides")
                        ),
                    ],
                ),
            ),
        ),
    )
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=namespace),
        spec=k8s.ServiceSpec(selector=_LABELS, ports=[_HTTP.k8s_service_port()], type="ClusterIP"),
    )
    k8s.KubeConfigMap(
        chart,
        "settingoverrides",
        metadata=k8s.ObjectMeta(name="grocy-settingoverrides", namespace=namespace),
        data={
            # Trust X-authentik-username header from Authentik outpost proxy.
            # Grocy reads this via PSR-7 getHeader(), which uses the raw HTTP header name.
            "AUTH_CLASS.txt": "Grocy\\Middleware\\ReverseProxyAuthMiddleware",
            "REVERSE_PROXY_AUTH_HEADER.txt": "X-authentik-username",
            # Default new (auto-created) Grocy users to NO permissions = read-only, so any
            # machine identity (haku) or unknown login is read-only by default — the safe
            # failure mode. Grocy's built-in default is ADMIN; "none" matches no
            # permission_hierarchy row, so CreateUser inserts an empty permission set (a safe
            # no-op in LessQL). Operators are elevated explicitly by the user-perms
            # reconciler (user_perms.py, instantiated per household).
            "DEFAULT_PERMISSIONS.txt": "none",
        },
    )
    # Restrict Grocy ingress to the Authentik shared proxy outpost only. Grocy trusts
    # X-authentik-username for user identity — any pod that can reach port 80 can
    # impersonate any user.
    k8s.KubeNetworkPolicy(
        chart,
        "networkpolicy",
        metadata=k8s.ObjectMeta(name="grocy-ingress", namespace=namespace),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels=_LABELS),
            policy_types=["Ingress"],
            ingress=[
                _from_namespace_pod(authentik.SERVER.pods.namespace, authentik.SERVER.pods.selector),
                # Gatus: health check probes
                _from_namespace_pod(cilium.PROBER.namespace, cilium.PROBER.selector),
            ],
        ),
    )


_MOVER_UID = 1000  # Matches the linuxserver.io image's PUID/PGID, so movers own the files they copy.


def _destination_mover_security_context() -> ReplicationDestinationSpecRsyncTlsMoverSecurityContext:
    return ReplicationDestinationSpecRsyncTlsMoverSecurityContext(
        run_as_user=_MOVER_UID,
        run_as_group=_MOVER_UID,
        fs_group=_MOVER_UID,
        seccomp_profile=ReplicationDestinationSpecRsyncTlsMoverSecurityContextSeccompProfile(type="RuntimeDefault"),
    )


def _source_mover_security_context() -> ReplicationSourceSpecRsyncTlsMoverSecurityContext:
    return ReplicationSourceSpecRsyncTlsMoverSecurityContext(
        run_as_user=_MOVER_UID,
        run_as_group=_MOVER_UID,
        fs_group=_MOVER_UID,
        seccomp_profile=ReplicationSourceSpecRsyncTlsMoverSecurityContextSeccompProfile(type="RuntimeDefault"),
    )


def _destination_mover_zone_affinity() -> ReplicationDestinationSpecRsyncTlsMoverAffinity:
    return ReplicationDestinationSpecRsyncTlsMoverAffinity(
        node_affinity=ReplicationDestinationSpecRsyncTlsMoverAffinityNodeAffinity(
            required_during_scheduling_ignored_during_execution=ReplicationDestinationSpecRsyncTlsMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                node_selector_terms=[
                    ReplicationDestinationSpecRsyncTlsMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms(
                        match_expressions=[
                            ReplicationDestinationSpecRsyncTlsMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions(
                                key=node_scheduling.ZONE_LABEL, operator="In", values=[node_scheduling.HIL_OVH_ZONE]
                            )
                        ]
                    )
                ]
            )
        )
    )


def _source_mover_zone_affinity() -> ReplicationSourceSpecRsyncTlsMoverAffinity:
    return ReplicationSourceSpecRsyncTlsMoverAffinity(
        node_affinity=ReplicationSourceSpecRsyncTlsMoverAffinityNodeAffinity(
            required_during_scheduling_ignored_during_execution=ReplicationSourceSpecRsyncTlsMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                node_selector_terms=[
                    ReplicationSourceSpecRsyncTlsMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms(
                        match_expressions=[
                            ReplicationSourceSpecRsyncTlsMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions(
                                key=node_scheduling.ZONE_LABEL, operator="In", values=[node_scheduling.HIL_OVH_ZONE]
                            )
                        ]
                    )
                ]
            )
        )
    )


def household_chart(app: App, *, household: str) -> Chart:
    namespace = service(household).pods.namespace
    chart = Chart(app, namespace, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=namespace, vpa=Vpa.AUTO)
    _server(chart, namespace)
    k8s.KubePersistentVolumeClaim(
        chart,
        "config-claim",
        metadata=k8s.ObjectMeta(name=_CONFIG_CLAIM, namespace=namespace),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="local-path-ovh",
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("1Gi")}),
        ),
    )
    k8s.KubeJob(
        chart,
        "verify-job",
        metadata=k8s.ObjectMeta(name="grocy-config-ovh-verify-20260520", namespace=namespace),
        spec=k8s.JobSpec(
            backoff_limit=0,
            template=k8s.PodTemplateSpec(
                # TODO: state automount_service_account_token=False once this Job is renamed or
                # removed; a Job's pod template is immutable, and without Flux's force annotation
                # changing it in place fails the apply.
                spec=k8s.PodSpec(
                    restart_policy="Never",
                    node_selector=node_scheduling.HIL_OVH_NODE_SELECTOR,
                    security_context=k8s.PodSecurityContext(seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault")),
                    containers=[
                        k8s.Container(
                            name="verifier",
                            image=_IMAGE,
                            command=["/bin/sh", "-ceu"],
                            args=[
                                "test -f /config/data/grocy.db\n"
                                'php -r \'$db=new PDO("sqlite:/config/data/grocy.db"); echo'
                                ' $db->query("PRAGMA integrity_check")->fetchColumn(), "\\n";\' | grep -Fx ok\n'
                            ],
                            volume_mounts=[k8s.VolumeMount(name="config", mount_path="/config")],
                        )
                    ],
                    volumes=[
                        k8s.Volume(
                            name="config",
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=_CONFIG_CLAIM),
                        )
                    ],
                )
            ),
        ),
    )
    ReplicationDestination(
        chart,
        "migration",
        metadata=ApiObjectMetadata(name="grocy-config-ovh-migration", namespace=namespace),
        trigger=ReplicationDestinationSpecTrigger(manual="prep-20260520"),
        rsync_tls=ReplicationDestinationSpecRsyncTls(
            destination_pvc=_CONFIG_CLAIM,
            copy_method=ReplicationDestinationSpecRsyncTlsCopyMethod.DIRECT,
            service_type="ClusterIP",
            mover_security_context=_destination_mover_security_context(),
        ),
    )
    # Periodic backup of the grocy-config-ovh PVC into a SeaweedFS-backed PVC.
    #
    # Direction: OVH local-path (primary, hot) → seaweedfs-ovh (cold, SeaweedFS replicates
    # across kimsufi nodes). The backup PVC is a restore point before any node-rename
    # operation that touches grocy-config-ovh's pinned hostname.
    #
    # copyMethod: Direct (no VolumeSnapshotClass installed). Mover and live grocy pod both
    # bind-mount the same host path; backups may catch torn SQLite writes occasionally —
    # acceptable for crash-consistent restore.
    k8s.KubePersistentVolumeClaim(
        chart,
        "backup-claim",
        metadata=k8s.ObjectMeta(name=_BACKUP, namespace=namespace),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="seaweedfs-ovh",
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("2Gi")}),
        ),
    )
    ReplicationDestination(
        chart,
        "backup-destination",
        metadata=ApiObjectMetadata(name=_BACKUP, namespace=namespace),
        rsync_tls=ReplicationDestinationSpecRsyncTls(
            destination_pvc=_BACKUP,
            copy_method=ReplicationDestinationSpecRsyncTlsCopyMethod.DIRECT,
            service_type="ClusterIP",
            mover_security_context=_destination_mover_security_context(),
            mover_affinity=_destination_mover_zone_affinity(),
        ),
    )
    ReplicationSource(
        chart,
        "backup-source",
        metadata=ApiObjectMetadata(name=_BACKUP, namespace=namespace),
        source_pvc=_CONFIG_CLAIM,
        trigger=ReplicationSourceSpecTrigger(schedule=_BACKUP_SCHEDULE),
        mover=ReplicationSourceSpecRsyncTls(
            copy_method=ReplicationSourceSpecRsyncTlsCopyMethod.DIRECT,
            key_secret=f"volsync-rsync-tls-{_BACKUP}",
            address=f"volsync-rsync-tls-dst-{_BACKUP}.{namespace}.svc",
            port=8000,
            mover_security_context=_source_mover_security_context(),
            mover_affinity=_source_mover_zone_affinity(),
        ),
    )
    return chart


def write_manifests(root: Path, household: str, *, mcp_dir: str) -> None:
    """The household's chart, and a `kustomization.yaml` that also includes its MCP server's `mcp_dir`."""
    directory = output_dir(household)
    write_yaml(
        root / directory / "kustomization.yaml",
        kustomize_kustomization(
            resources=[
                posixpath.relpath(mcp_dir, directory),
                write_charts(root, directory, partial(household_chart, household=household)),
            ]
        ),
    )
