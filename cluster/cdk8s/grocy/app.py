"""Grocy itself: the household-independent `app-base` (Deployment, Service, settings
overrides, ingress NetworkPolicy) and each household's `<household>/app` (Namespace,
config PVC, and the VolSync backup, migration and verification objects around it).

Hand-written beside the generated output: each household's `app/kustomization.yaml`, whose
patch moves the Deployment onto the hil-ovh zone.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from volsync_replicationdestination_crds.backube.volsync import (
    ReplicationDestination,
    ReplicationDestinationSpec,
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
    ReplicationSource,
    ReplicationSourceSpec,
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

from cluster.cdk8s.flux import kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata

BASE_DIR = f"{HAND_WRITTEN_ROOT}/grocy/app-base"
_NAME = "grocy"
_LABELS = {"app.kubernetes.io/name": _NAME}
_IMAGE = "lscr.io/linuxserver/grocy:v4.6.0-ls318"
_CONFIG_CLAIM = "grocy-config-ovh"
_BACKUP = "grocy-config-ovh-backup"
_ZONE = "hil-ovh"
_ZONE_KEY = "topology.kubernetes.io/zone"
_HTTP_PORT = 80


def _from_namespace_pod(namespace: str, pod_labels: dict[str, str]) -> k8s.NetworkPolicyIngressRule:
    return k8s.NetworkPolicyIngressRule(
        from_=[
            k8s.NetworkPolicyPeer(
                namespace_selector=k8s.LabelSelector(match_labels={"kubernetes.io/metadata.name": namespace}),
                pod_selector=k8s.LabelSelector(match_labels=pod_labels),
            )
        ],
        ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(_HTTP_PORT), protocol="TCP")],
    )


def base_chart(app: App) -> Chart:
    """The objects every household runs; the household overlay supplies the namespace."""
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    login_probe = k8s.HttpGetAction(path="/login", port=k8s.IntOrString.from_number(_HTTP_PORT))
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(name=_NAME, labels=_LABELS, annotations={"reloader.stakater.com/auto": "true"}),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=_LABELS),
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    node_selector={"topology.kubernetes.io/region": "hil"},
                    containers=[
                        k8s.Container(
                            name=_NAME,
                            image=_IMAGE,
                            ports=[k8s.ContainerPort(container_port=_HTTP_PORT, name="http")],
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
        metadata=k8s.ObjectMeta(name=_NAME),
        spec=k8s.ServiceSpec(
            selector=_LABELS,
            ports=[
                k8s.ServicePort(
                    name="http", port=_HTTP_PORT, target_port=k8s.IntOrString.from_number(_HTTP_PORT), protocol="TCP"
                )
            ],
            type="ClusterIP",
        ),
    )
    k8s.KubeConfigMap(
        chart,
        "settingoverrides",
        metadata=k8s.ObjectMeta(name="grocy-settingoverrides"),
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
        metadata=k8s.ObjectMeta(name="grocy-ingress"),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels=_LABELS),
            policy_types=["Ingress"],
            ingress=[
                _from_namespace_pod(
                    "authentik", {"app.kubernetes.io/component": "server", "app.kubernetes.io/name": "authentik"}
                ),
                # Gatus: health check probes
                _from_namespace_pod("gatus", {"app.kubernetes.io/name": "gatus"}),
            ],
        ),
    )
    return chart


def household_chart(app: App, *, household: str, backup_schedule: str) -> Chart:
    namespace = f"grocy-{household}"
    chart = Chart(app, namespace, disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=namespace,
            labels={
                "goldilocks.fairwinds.com/enabled": "true",
                "goldilocks.fairwinds.com/vpa-update-mode": "auto",
                "rbac.ducktape.io/agent-readable-logs": "true",
            },
        ),
    )
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
                spec=k8s.PodSpec(
                    restart_policy="Never",
                    node_selector={_ZONE_KEY: _ZONE},
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
        metadata=metadata("grocy-config-ovh-migration", namespace),
        spec=ReplicationDestinationSpec(
            trigger=ReplicationDestinationSpecTrigger(manual="prep-20260520"),
            rsync_tls=ReplicationDestinationSpecRsyncTls(
                destination_pvc=_CONFIG_CLAIM,
                copy_method=ReplicationDestinationSpecRsyncTlsCopyMethod.DIRECT,
                service_type="ClusterIP",
                mover_security_context=ReplicationDestinationSpecRsyncTlsMoverSecurityContext(
                    run_as_user=1000,
                    run_as_group=1000,
                    fs_group=1000,
                    seccomp_profile=ReplicationDestinationSpecRsyncTlsMoverSecurityContextSeccompProfile(
                        type="RuntimeDefault"
                    ),
                ),
            ),
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
        metadata=metadata(_BACKUP, namespace),
        spec=ReplicationDestinationSpec(
            rsync_tls=ReplicationDestinationSpecRsyncTls(
                destination_pvc=_BACKUP,
                copy_method=ReplicationDestinationSpecRsyncTlsCopyMethod.DIRECT,
                service_type="ClusterIP",
                mover_security_context=ReplicationDestinationSpecRsyncTlsMoverSecurityContext(
                    run_as_user=1000,
                    run_as_group=1000,
                    fs_group=1000,
                    seccomp_profile=ReplicationDestinationSpecRsyncTlsMoverSecurityContextSeccompProfile(
                        type="RuntimeDefault"
                    ),
                ),
                mover_affinity=ReplicationDestinationSpecRsyncTlsMoverAffinity(
                    node_affinity=ReplicationDestinationSpecRsyncTlsMoverAffinityNodeAffinity(
                        required_during_scheduling_ignored_during_execution=ReplicationDestinationSpecRsyncTlsMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                            node_selector_terms=[
                                ReplicationDestinationSpecRsyncTlsMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms(
                                    match_expressions=[
                                        ReplicationDestinationSpecRsyncTlsMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions(
                                            key=_ZONE_KEY, operator="In", values=[_ZONE]
                                        )
                                    ]
                                )
                            ]
                        )
                    )
                ),
            )
        ),
    )
    ReplicationSource(
        chart,
        "backup-source",
        metadata=metadata(_BACKUP, namespace),
        spec=ReplicationSourceSpec(
            source_pvc=_CONFIG_CLAIM,
            trigger=ReplicationSourceSpecTrigger(schedule=backup_schedule),
            rsync_tls=ReplicationSourceSpecRsyncTls(
                copy_method=ReplicationSourceSpecRsyncTlsCopyMethod.DIRECT,
                key_secret=f"volsync-rsync-tls-{_BACKUP}",
                address=f"volsync-rsync-tls-dst-{_BACKUP}.{namespace}.svc",
                port=8000,
                mover_security_context=ReplicationSourceSpecRsyncTlsMoverSecurityContext(
                    run_as_user=1000,
                    run_as_group=1000,
                    fs_group=1000,
                    seccomp_profile=ReplicationSourceSpecRsyncTlsMoverSecurityContextSeccompProfile(
                        type="RuntimeDefault"
                    ),
                ),
                mover_affinity=ReplicationSourceSpecRsyncTlsMoverAffinity(
                    node_affinity=ReplicationSourceSpecRsyncTlsMoverAffinityNodeAffinity(
                        required_during_scheduling_ignored_during_execution=ReplicationSourceSpecRsyncTlsMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                            node_selector_terms=[
                                ReplicationSourceSpecRsyncTlsMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms(
                                    match_expressions=[
                                        ReplicationSourceSpecRsyncTlsMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions(
                                            key=_ZONE_KEY, operator="In", values=[_ZONE]
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
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, BASE_DIR, base_chart)
    write_yaml(root / BASE_DIR / "kustomization.yaml", kustomize_kustomization(resources=[f"{_NAME}.k8s.yaml"]))
    write_charts(
        root,
        f"{HAND_WRITTEN_ROOT}/grocy/sf/app",
        lambda app: household_chart(app, household="sf", backup_schedule="23 */6 * * *"),
    )
    write_charts(
        root,
        f"{HAND_WRITTEN_ROOT}/grocy/vallejo/app",
        lambda app: household_chart(app, household="vallejo", backup_schedule="29 */6 * * *"),
    )
