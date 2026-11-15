"""Active-on-deployment, fixed-name wyrm2 replay Job; no retries or TTL."""

import hashlib
import json

from cdk8s import ApiObjectMetadata, Chart
from cdk8s_plus_34 import ConfigMap, k8s
from python.runfiles import Runfiles

from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy

NAME = "wyrm2-alloy-canary"
_NAMESPACE = "monitoring"


def add(chart: Chart, *, node_config: str, journal_config: str) -> None:
    script = ConfigMap(
        chart,
        NAME + "-script",
        metadata=ApiObjectMetadata(name=NAME + "-script", namespace=_NAMESPACE),
        data={
            "cutover.py": (
                Runfiles.CreateOrRaise().root() / "ducktape/cluster/cdk8s/monitoring/wyrm2_cutover.py"
            ).read_text(),
            "config-hashes.json": json.dumps(
                {
                    "alloy-node-config": hashlib.sha256(node_config.encode()).hexdigest(),
                    "alloy-journal-config": hashlib.sha256(journal_config.encode()).hexdigest(),
                }
            ),
        },
    )
    k8s.KubeServiceAccount(chart, NAME + "-account", metadata=k8s.ObjectMeta(name=NAME, namespace=_NAMESPACE))
    subject = k8s.Subject(kind="ServiceAccount", name=NAME, namespace=_NAMESPACE)
    k8s.KubeClusterRole(
        chart,
        NAME + "-node-role",
        metadata=k8s.ObjectMeta(name=NAME),
        rules=[k8s.PolicyRule(api_groups=[""], resources=["nodes"], resource_names=["wyrm2"], verbs=["get", "patch"])],
    )
    k8s.KubeClusterRoleBinding(
        chart,
        NAME + "-node-binding",
        metadata=k8s.ObjectMeta(name=NAME),
        subjects=[subject],
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="ClusterRole", name=NAME),
    )
    for namespace, names in (
        ("loki", ["promtail", "promtail-journal"]),
        ("monitoring", ["alloy-node", "alloy-journal"]),
    ):
        rules = [
            k8s.PolicyRule(api_groups=[""], resources=["pods"], verbs=["list"]),
            k8s.PolicyRule(api_groups=["apps"], resources=["daemonsets"], resource_names=names, verbs=["get"]),
        ]
        if namespace == _NAMESPACE:
            rules.append(
                k8s.PolicyRule(
                    api_groups=[""],
                    resources=["configmaps"],
                    resource_names=["alloy-node-config", "alloy-journal-config"],
                    verbs=["get"],
                )
            )
        k8s.KubeRole(
            chart, NAME + "-role-" + namespace, metadata=k8s.ObjectMeta(name=NAME, namespace=namespace), rules=rules
        )
        k8s.KubeRoleBinding(
            chart,
            NAME + "-binding-" + namespace,
            metadata=k8s.ObjectMeta(name=NAME, namespace=namespace),
            subjects=[subject],
            role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=NAME),
        )
    labels = {"app.kubernetes.io/name": NAME}
    NetworkPolicy(
        chart,
        NAME + "-network",
        metadata=ApiObjectMetadata(name=NAME, namespace=_NAMESPACE),
        endpoint_selector=labels,
        ingress=[],
        egress=[EgressRule.to_entities(Entity.KUBE_APISERVER)],
    )
    host_mounts = (
        ("node", "/var/lib/alloy-node", "/state/node", "Directory", True),
        ("journal", "/var/lib/alloy-journal", "/state/journal", "DirectoryOrCreate", True),
        ("pods", "/var/lib/alloy-node/loki.source.file.pods", "/state/pods", "DirectoryOrCreate", False),
        ("promtail", "/run/promtail", "/state/promtail", "Directory", True),
        ("evidence", "/var/lib/promtail-alloy-wyrm2-canary", "/evidence", "DirectoryOrCreate", False),
    )
    k8s.KubeJob(
        chart,
        NAME,
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=_NAMESPACE,
            annotations={
                "description": "Deploying runs retained-log replay on wyrm2: pod duplicates and up to 12h journal replay. "
                "No automatic rollback; see cluster/cdk8s/monitoring/docs/wyrm2-alloy-canary.md."
            },
        ),
        spec=k8s.JobSpec(
            backoff_limit=0,
            active_deadline_seconds=900,
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=labels),
                spec=k8s.PodSpec(
                    node_name="wyrm2",
                    service_account_name=NAME,
                    automount_service_account_token=True,
                    restart_policy="Never",
                    security_context=k8s.PodSecurityContext(
                        run_as_user=0, seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault")
                    ),
                    containers=[
                        k8s.Container(
                            name="cutover",
                            image="python:3.14-alpine",
                            command=["python", "-B", "/scripts/cutover.py"],
                            env=[
                                k8s.EnvVar(
                                    name="NODE_NAME",
                                    value_from=k8s.EnvVarSource(
                                        field_ref=k8s.ObjectFieldSelector(field_path="spec.nodeName")
                                    ),
                                )
                            ],
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False,
                                read_only_root_filesystem=True,
                                capabilities=k8s.Capabilities(drop=["ALL"]),
                            ),
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("20m"),
                                    "memory": k8s.Quantity.from_string("64Mi"),
                                },
                                limits={"memory": k8s.Quantity.from_string("128Mi")},
                            ),
                            volume_mounts=[
                                k8s.VolumeMount(name=name, mount_path=mount, read_only=read_only)
                                for name, _, mount, _, read_only in host_mounts
                            ]
                            + [k8s.VolumeMount(name="script", mount_path="/scripts", read_only=True)],
                        )
                    ],
                    volumes=[
                        k8s.Volume(name=name, host_path=k8s.HostPathVolumeSource(path=path, type=kind))
                        for name, path, _, kind, _ in host_mounts
                    ]
                    + [k8s.Volume(name="script", config_map=k8s.ConfigMapVolumeSource(name=script.name))],
                ),
            ),
        ),
    )
