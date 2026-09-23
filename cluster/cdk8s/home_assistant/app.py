"""Home Assistant: the Deployment with its Caddy metrics proxy, the onboarding Job, the
config volume and its VolSync backup, pull credentials, metrics token, route, the backup
mover's egress policy, and the ServiceMonitor and PrometheusRule.

The provisioner image tag is the placeholder "unset"; the hand-written
`cluster/k8s/home-assistant/app/image-pins/kustomization.yaml` overrides it at
`kustomize build` time via Flux's image-automation marker. Hand-written beside the generated
output: the `configMapGenerator` inputs, the SOPS break-glass Secret and the `kustomization.yaml` that generates the ConfigMaps.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct
from eso_password_generator_crds.io.external_secrets.generators import Password, PasswordSpec
from external_secrets_crds.io.external_secrets import (
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecDataFrom,
    ExternalSecretSpecDataFromSourceRef,
    ExternalSecretSpecDataFromSourceRefGeneratorRef,
    ExternalSecretSpecDataFromSourceRefGeneratorRefKind,
    ExternalSecretSpecRefreshPolicy,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetCreationPolicy,
)
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitor,
    ServiceMonitorSpec,
    ServiceMonitorSpecEndpoints,
    ServiceMonitorSpecEndpointsAuthorization,
    ServiceMonitorSpecEndpointsAuthorizationCredentials,
    ServiceMonitorSpecSelector,
)
from prometheus_operator_prometheusrule_crds.com.coreos.monitoring import (
    PrometheusRule,
    PrometheusRuleSpec,
    PrometheusRuleSpecGroups,
    PrometheusRuleSpecGroupsRules,
    PrometheusRuleSpecGroupsRulesExpr,
)
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

from cluster.cdk8s.forgejo_images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

_OUTPUT_DIR = "cluster/k8s/home-assistant/app"
_NAME = "home-assistant"
_NAMESPACE = "home-assistant"
_LABELS = {"app.kubernetes.io/name": _NAME}
_NODE_SELECTOR = {"kubernetes.io/hostname": "optiplex"}
_CONFIG_CLAIM = "home-assistant-config"
_METRICS_TOKEN = "home-assistant-metrics-token"
_BACKUP = "home-assistant-config-restic"
_BACKUP_LABELS = {"app.kubernetes.io/name": _BACKUP}
_STORAGE_CLASS = "local-path-home-ssd"
_PROVISIONER_IMAGE = "git.allegedly.works/ducktape-ci/homeassistant-provisioner:unset"
_PROVISIONER_COMMAND = ["/homeassistant/provisioner/provision_bin"]
_PROVISIONER_CONFIG = "/etc/homeassistant/provisioner.yaml"
# Rendered by the kustomization.yaml's configMapGenerator.
_CONFIGURATION_CONFIG_MAP = "home-assistant-configuration"
_PROVISIONER_CONFIG_MAP = "home-assistant-provisioner-config"
_CADDY_CONFIG_MAP = "home-assistant-caddy"


def _quantities(**values: str) -> dict[str, k8s.Quantity]:
    return {key: k8s.Quantity.from_string(value) for key, value in values.items()}


def _provisioner_mounts() -> list[k8s.VolumeMount]:
    return [
        k8s.VolumeMount(name="config", mount_path="/config"),
        k8s.VolumeMount(
            name="provisioner-config", mount_path=_PROVISIONER_CONFIG, sub_path="provisioner.yaml", read_only=True
        ),
    ]


def _config_volume() -> k8s.Volume:
    return k8s.Volume(
        name="config", persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=_CONFIG_CLAIM)
    )


def _config_map_volume(name: str, config_map: str) -> k8s.Volume:
    return k8s.Volume(name=name, config_map=k8s.ConfigMapVolumeSource(name=config_map))


def _backend_probe(*, initial_delay_seconds: int, period_seconds: int) -> k8s.Probe:
    return k8s.Probe(
        http_get=k8s.HttpGetAction(host="127.0.0.1", port=k8s.IntOrString.from_string("backend"), path="/"),
        initial_delay_seconds=initial_delay_seconds,
        period_seconds=period_seconds,
    )


def _deployment(scope: Construct) -> None:
    k8s.KubeDeployment(
        scope,
        "deployment",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=_NAMESPACE, labels=_LABELS),
        spec=k8s.DeploymentSpec(
            replicas=1,
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    host_network=True,
                    dns_policy="ClusterFirstWithHostNet",
                    node_selector=_NODE_SELECTOR,
                    init_containers=[
                        k8s.Container(
                            name="provision-components",
                            image=_PROVISIONER_IMAGE,
                            image_pull_policy="Always",
                            command=_PROVISIONER_COMMAND,
                            env=[
                                k8s.EnvVar(name="HOME_ASSISTANT_PROVISIONER_CONFIG_FILE", value=_PROVISIONER_CONFIG),
                                k8s.EnvVar(name="HOME_ASSISTANT_PROVISIONER_ONBOARDING_ENABLED", value="false"),
                            ],
                            volume_mounts=_provisioner_mounts(),
                        )
                    ],
                    containers=[
                        k8s.Container(
                            name=_NAME,
                            image="ghcr.io/home-assistant/home-assistant:2026.9.2",
                            env=[
                                k8s.EnvVar(name="TZ", value="America/Los_Angeles"),
                                # Home Assistant's HTTP settings are converged through the admin
                                # API after startup. This keeps the empty-PVC bootstrap port
                                # aligned with Caddy while the API applies the loopback/proxy
                                # settings.
                                k8s.EnvVar(name="SETUP_PORT", value="8124"),
                            ],
                            ports=[k8s.ContainerPort(name="backend", container_port=8124)],
                            readiness_probe=_backend_probe(initial_delay_seconds=15, period_seconds=10),
                            liveness_probe=_backend_probe(initial_delay_seconds=60, period_seconds=30),
                            resources=k8s.ResourceRequirements(
                                requests=_quantities(cpu="250m", memory="512Mi"), limits=_quantities(memory="2Gi")
                            ),
                            volume_mounts=[
                                k8s.VolumeMount(name="config", mount_path="/config"),
                                k8s.VolumeMount(
                                    name="configuration",
                                    mount_path="/config/configuration.yaml",
                                    sub_path="configuration.yaml",
                                    read_only=True,
                                ),
                            ],
                        ),
                        k8s.Container(
                            name="caddy",
                            image="caddy:2.11.4-alpine",
                            env=[
                                k8s.EnvVar(
                                    name="METRICS_TOKEN",
                                    value_from=k8s.EnvVarSource(
                                        secret_key_ref=k8s.SecretKeySelector(name=_METRICS_TOKEN, key="password")
                                    ),
                                )
                            ],
                            ports=[k8s.ContainerPort(name="http", container_port=8123)],
                            readiness_probe=k8s.Probe(
                                http_get=k8s.HttpGetAction(port=k8s.IntOrString.from_string("http"), path="/"),
                                period_seconds=10,
                            ),
                            resources=k8s.ResourceRequirements(
                                requests=_quantities(cpu="20m", memory="32Mi"), limits=_quantities(memory="128Mi")
                            ),
                            volume_mounts=[
                                k8s.VolumeMount(
                                    name="caddy-config",
                                    mount_path="/etc/caddy/Caddyfile",
                                    sub_path="Caddyfile",
                                    read_only=True,
                                )
                            ],
                        ),
                    ],
                    volumes=[
                        _config_volume(),
                        _config_map_volume("configuration", _CONFIGURATION_CONFIG_MAP),
                        _config_map_volume("provisioner-config", _PROVISIONER_CONFIG_MAP),
                        _config_map_volume("caddy-config", _CADDY_CONFIG_MAP),
                    ],
                ),
            ),
        ),
    )
    k8s.KubeService(
        scope,
        "service",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=_NAMESPACE, labels=_LABELS),
        spec=k8s.ServiceSpec(
            selector=_LABELS,
            ports=[k8s.ServicePort(name="http", port=8123, target_port=k8s.IntOrString.from_string("http"))],
        ),
    )


def _onboarding_job(scope: Construct) -> None:
    k8s.KubeJob(
        scope,
        "onboarding",
        metadata=k8s.ObjectMeta(
            name="home-assistant-onboarding",
            namespace=_NAMESPACE,
            annotations={"kustomize.toolkit.fluxcd.io/force": "enabled"},
        ),
        spec=k8s.JobSpec(
            backoff_limit=3,
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(
                    # Bump when bootstrap behavior changes so Flux replaces the immutable Job.
                    annotations={"home-assistant.allegedly.works/bootstrap-revision": "3"},
                    labels={"app.kubernetes.io/name": "home-assistant-onboarding"},
                ),
                spec=k8s.PodSpec(
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    restart_policy="OnFailure",
                    node_selector=_NODE_SELECTOR,
                    containers=[
                        k8s.Container(
                            name="onboarding",
                            image=_PROVISIONER_IMAGE,
                            image_pull_policy="Always",
                            command=_PROVISIONER_COMMAND,
                            env=[
                                k8s.EnvVar(name="HOME_ASSISTANT_PROVISIONER_CONFIG_FILE", value=_PROVISIONER_CONFIG),
                                k8s.EnvVar(
                                    name="HOME_ASSISTANT_PROVISIONER_LOCAL_ADMIN_PASSWORD",
                                    value_from=k8s.EnvVarSource(
                                        secret_key_ref=k8s.SecretKeySelector(
                                            name="home-assistant-break-glass", key="password"
                                        )
                                    ),
                                ),
                            ],
                            resources=k8s.ResourceRequirements(
                                requests=_quantities(cpu="20m", memory="64Mi"), limits=_quantities(memory="256Mi")
                            ),
                            volume_mounts=_provisioner_mounts(),
                        )
                    ],
                    volumes=[_config_volume(), _config_map_volume("provisioner-config", _PROVISIONER_CONFIG_MAP)],
                ),
            ),
        ),
    )


def _metrics_token(scope: Construct) -> None:
    generator = Password(
        scope,
        "metrics-token-generator",
        metadata=metadata(_METRICS_TOKEN, _NAMESPACE),
        spec=PasswordSpec(length=48, digits=12, symbols=0, no_upper=False, allow_repeat=True),
    )
    ExternalSecret(
        scope,
        "metrics-token",
        metadata=metadata(_METRICS_TOKEN, _NAMESPACE),
        spec=ExternalSecretSpec(
            refresh_policy=ExternalSecretSpecRefreshPolicy.CREATED_ONCE,
            target=ExternalSecretSpecTarget(
                name=_METRICS_TOKEN, creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER
            ),
            data_from=[
                ExternalSecretSpecDataFrom(
                    source_ref=ExternalSecretSpecDataFromSourceRef(
                        generator_ref=ExternalSecretSpecDataFromSourceRefGeneratorRef(
                            api_version="generators.external-secrets.io/v1alpha1",
                            kind=ExternalSecretSpecDataFromSourceRefGeneratorRefKind.PASSWORD,
                            name=generator.name,
                        )
                    )
                )
            ],
        ),
    )


def _monitoring(scope: Construct) -> None:
    ServiceMonitor(
        scope,
        "service-monitor",
        metadata=metadata(_NAME, _NAMESPACE),
        spec=ServiceMonitorSpec(
            selector=ServiceMonitorSpecSelector(match_labels=_LABELS),
            endpoints=[
                ServiceMonitorSpecEndpoints(
                    port="http",
                    path="/api/prometheus",
                    authorization=ServiceMonitorSpecEndpointsAuthorization(
                        type="Bearer",
                        credentials=ServiceMonitorSpecEndpointsAuthorizationCredentials(
                            name=_METRICS_TOKEN, key="password"
                        ),
                    ),
                )
            ],
        ),
    )
    PrometheusRule(
        scope,
        "prometheus-rule",
        metadata=metadata(_NAME, _NAMESPACE, labels={"release": "kube-prometheus-stack"}),
        spec=PrometheusRuleSpec(
            groups=[
                PrometheusRuleSpecGroups(
                    name=_NAME,
                    rules=[
                        PrometheusRuleSpecGroupsRules(
                            alert="HomeAssistantUnavailable",
                            expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
                                'up{namespace="home-assistant", service="home-assistant"} == 0'
                            ),
                            for_="10m",
                            labels={"severity": "warning"},
                            annotations={
                                "summary": "Home Assistant is unavailable",
                                "description": "Prometheus has been unable to scrape Home Assistant for 10 minutes.",
                            },
                        )
                    ],
                )
            ]
        ),
    )


def _backup(scope: Construct) -> None:
    # Encrypted, deduplicated snapshots of Home Assistant's OptiPlex-local state in the
    # dedicated SeaweedFS S3 bucket. Home Assistant's own backup integration remains the
    # source of application-consistent restore points in this PVC.
    ReplicationSource(
        scope,
        "backup",
        metadata=metadata(_BACKUP, _NAMESPACE),
        spec=ReplicationSourceSpec(
            source_pvc=_CONFIG_CLAIM,
            trigger=ReplicationSourceSpecTrigger(schedule="17 */6 * * *"),
            restic=ReplicationSourceSpecRestic(
                repository="home-assistant-config-restic-tenant",
                copy_method=ReplicationSourceSpecResticCopyMethod.DIRECT,
                prune_interval_days=7,
                retain=ReplicationSourceSpecResticRetain(daily=7, weekly=4, monthly=6),
                cache_storage_class_name=_STORAGE_CLASS,
                cache_access_modes=["ReadWriteOnce"],
                cache_capacity=ReplicationSourceSpecResticCacheCapacity.from_string("1Gi"),
                mover_pod_labels=_BACKUP_LABELS,
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
                    run_as_user=0,
                    run_as_group=0,
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
                                            key="kubernetes.io/hostname", operator="In", values=["optiplex"]
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
    k8s.KubeNetworkPolicy(
        scope,
        "backup-egress",
        metadata=k8s.ObjectMeta(name=f"{_BACKUP}-egress", namespace=_NAMESPACE),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels=_BACKUP_LABELS),
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


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    k8s.KubePersistentVolumeClaim(
        chart,
        "config",
        metadata=k8s.ObjectMeta(name=_CONFIG_CLAIM, namespace=_NAMESPACE),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name=_STORAGE_CLASS,
            resources=k8s.VolumeResourceRequirements(requests=_quantities(storage="32Gi")),
        ),
    )
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=_NAMESPACE)
    _metrics_token(chart)
    _deployment(chart)
    _onboarding_job(chart)
    https_route(
        chart,
        "route",
        metadata=metadata(_NAME, _NAMESPACE),
        hostname="home.allegedly.works",
        backend=_NAME,
        port=8123,
        hsts=False,
        listener=None,
    )
    _backup(chart)
    _monitoring(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, _OUTPUT_DIR, chart)
