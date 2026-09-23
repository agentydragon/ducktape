"""Home Assistant: the Deployment with its Caddy metrics proxy, the onboarding Job, the CronJob
that keeps other workloads' tokens valid, the config volume and its VolSync backup, pull
credentials, metrics token, route, the backup mover's egress policy, and the ServiceMonitor and
PrometheusRule.

The provisioner images' tags are the placeholder "unset"; the hand-written
`cluster/k8s/home-assistant/app/image-pins/kustomization.yaml` overrides them at
`kustomize build` time via Flux's image-automation markers. Hand-written beside the generated
output: the `configMapGenerator` inputs, the SOPS break-glass Secret and the `kustomization.yaml` that generates the ConfigMaps.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct
from eso_password_generator_crds.io.external_secrets.generators import Password, PasswordSpec
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecRefreshPolicy,
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

from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.external_secrets.external_secret import add_external_secret, password_generator
from cluster.cdk8s.forgejo_images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

# Aliased: each provisioner names its model `Settings`, in a module named `settings`.
from homeassistant.provisioner.components import settings as components
from homeassistant.provisioner.endpoint import HomeAssistantEndpoint
from homeassistant.provisioner.onboarding import settings as onboarding
from homeassistant.provisioner.tokens import settings as tokens
from homeassistant.provisioner.yaml_settings import YamlFileSettings
from util.settings_contract import env_name, settings_file

_OUTPUT_DIR = "cluster/k8s/home-assistant/app"
_NAME = "home-assistant"
_NAMESPACE = "home-assistant"
_HOSTNAME = "home.allegedly.works"
_LABELS = {"app.kubernetes.io/name": _NAME}
_NODE_SELECTOR = {"kubernetes.io/hostname": "optiplex"}
_CONFIG_CLAIM = "home-assistant-config"
_METRICS_TOKEN = "home-assistant-metrics-token"
_BACKUP = "home-assistant-config-restic"
_BACKUP_LABELS = {"app.kubernetes.io/name": _BACKUP}
_STORAGE_CLASS = "local-path-home-ssd"
_ONBOARDING = "home-assistant-onboarding"
_TOKEN_PROVISIONER = "home-assistant-token-provisioner"
_COMPONENT_INSTALLER_IMAGE = "git.allegedly.works/ducktape-ci/homeassistant-component-installer:unset"
_ONBOARDING_IMAGE = "git.allegedly.works/ducktape-ci/homeassistant-onboarding:unset"
_TOKEN_PROVISIONER_IMAGE = "git.allegedly.works/ducktape-ci/homeassistant-token-provisioner:unset"
# Each provisioner container mounts its settings ConfigMap here.
_SETTINGS_DIR = "/etc/provisioner"
# Where the config volume mounts, in Home Assistant and in its component installer.
_CONFIG_DIR = "/config"
# Home Assistant's own listener; Caddy (the hand-written Caddyfile) proxies to it.
_BACKEND_PORT = 8124
# Rendered by the kustomization.yaml's configMapGenerator.
_CONFIGURATION_CONFIG_MAP = "home-assistant-configuration"
_CADDY_CONFIG_MAP = "home-assistant-caddy"

# The local owner the provisioners log in as, through the in-cluster Service.
_ENDPOINT = HomeAssistantEndpoint(
    url=f"http://{_NAME}.{_NAMESPACE}.svc.cluster.local:8123",
    client_id=f"https://{_HOSTNAME}/",
    redirect_uri=f"https://{_HOSTNAME}/",
)
_OWNER_USERNAME = "ha-local-admin"

# Custom components the init container installs before Home Assistant starts.
_COMPONENTS = (
    components.ComponentConfig(
        version="2.2.1",
        url="https://github.com/dreo-team/hass-dreoverse/archive/refs/tags/v2.2.1.zip",
        sha256="555e4e3470b64574bfe5a050ca5557ce3f509844e2e8827ecd7e10c8a3d77d24",
        archive_path="*/custom_components/dreo",
        install_dir="dreo",
        manifest_domain="dreo",
    ),
    components.ComponentConfig(
        version="1.1.1",
        url="https://github.com/christiaangoossens/hass-oidc-auth/releases/download/v1.1.1/hass-oidc-auth.zip",
        sha256="9ce9e6153f80c781e360b93e097ff7d87d09235430fc48e7a67d97dda5fc3322",
        archive_path=".",
        install_dir="auth_oidc",
        config_files=("automations.yaml", "scripts.yaml", "scenes.yaml"),
    ),
)

# The long-lived tokens the provisioner keeps valid. It writes only in this namespace; a workload
# holding one copies it with ESO.
HA_MCP_TOKEN = tokens.TokenConfig(
    client_name="ha-mcp-cluster",
    secret_name="ha-mcp-home-assistant-token",
    secret_namespace=_NAMESPACE,
    description="Long-lived token of Home Assistant's local owner, which ha-mcp copies",
)
AGENTPLANE_READER_TOKEN = tokens.TokenConfig(
    client_name="agentplane-egress",
    read_only_user="agentplane-reader",
    secret_name="agentplane-home-assistant-token",
    secret_namespace=_NAMESPACE,
    description=(
        "Long-lived token of Home Assistant's read-only agentplane-reader user, which agentplane-staging's egress "
        "proxy presents"
    ),
)
_TOKENS = (HA_MCP_TOKEN, AGENTPLANE_READER_TOKEN)

_BREAK_GLASS_PASSWORD = k8s.EnvVarSource(
    secret_key_ref=k8s.SecretKeySelector(name="home-assistant-break-glass", key="password")
)


def _quantities(**values: str) -> dict[str, k8s.Quantity]:
    return {key: k8s.Quantity.from_string(value) for key, value in values.items()}


def _settings_config_map(
    scope: Construct,
    name: str,
    settings: type[YamlFileSettings],
    content: dict[str, object],
    *,
    supplied: tuple[tuple[str, ...], ...] = (),
) -> k8s.KubeConfigMap:
    """A provisioner's settings file, each key checked against `settings`; `supplied` names the
    fields an env var completes."""
    return k8s.KubeConfigMap(
        scope,
        name,
        metadata=k8s.ObjectMeta(name=name, namespace=_NAMESPACE),
        data={"settings.yaml": yaml_config(settings_file(settings, content, supplied=supplied))},
    )


def _settings_env(settings: type[YamlFileSettings]) -> k8s.EnvVar:
    return k8s.EnvVar(name=settings.config_file_env, value=f"{_SETTINGS_DIR}/settings.yaml")


def _settings_mount(volume: str) -> k8s.VolumeMount:
    return k8s.VolumeMount(name=volume, mount_path=_SETTINGS_DIR, read_only=True)


def _config_map_volume(name: str, config_map: str) -> k8s.Volume:
    return k8s.Volume(name=name, config_map=k8s.ConfigMapVolumeSource(name=config_map))


def _backend_probe(*, initial_delay_seconds: int, period_seconds: int) -> k8s.Probe:
    return k8s.Probe(
        http_get=k8s.HttpGetAction(host="127.0.0.1", port=k8s.IntOrString.from_string("backend"), path="/"),
        initial_delay_seconds=initial_delay_seconds,
        period_seconds=period_seconds,
    )


def _deployment(scope: Construct) -> None:
    installer_settings = _settings_config_map(
        scope,
        "home-assistant-component-installer",
        components.Settings,
        {
            "config_dir": _CONFIG_DIR,
            "components": [component.model_dump(mode="json", exclude_defaults=True) for component in _COMPONENTS],
        },
    )
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
                            image=_COMPONENT_INSTALLER_IMAGE,
                            image_pull_policy="Always",
                            command=["/homeassistant/provisioner/components/install_bin"],
                            env=[_settings_env(components.Settings)],
                            volume_mounts=[
                                k8s.VolumeMount(name="config", mount_path=_CONFIG_DIR),
                                _settings_mount("installer-settings"),
                            ],
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
                                k8s.EnvVar(name="SETUP_PORT", value=str(_BACKEND_PORT)),
                            ],
                            ports=[k8s.ContainerPort(name="backend", container_port=_BACKEND_PORT)],
                            readiness_probe=_backend_probe(initial_delay_seconds=15, period_seconds=10),
                            liveness_probe=_backend_probe(initial_delay_seconds=60, period_seconds=30),
                            resources=k8s.ResourceRequirements(
                                requests=_quantities(cpu="250m", memory="512Mi"), limits=_quantities(memory="2Gi")
                            ),
                            volume_mounts=[
                                k8s.VolumeMount(name="config", mount_path=_CONFIG_DIR),
                                k8s.VolumeMount(
                                    name="configuration",
                                    mount_path=f"{_CONFIG_DIR}/configuration.yaml",
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
                        k8s.Volume(
                            name="config",
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=_CONFIG_CLAIM),
                        ),
                        _config_map_volume("configuration", _CONFIGURATION_CONFIG_MAP),
                        _config_map_volume("installer-settings", installer_settings.name),
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
    settings = _settings_config_map(
        scope,
        _ONBOARDING,
        onboarding.Settings,
        {
            "endpoint": _ENDPOINT.model_dump(),
            "owner_username": _OWNER_USERNAME,
            "owner_display_name": "Home Assistant Local Administrator",
            "http_config": onboarding.HttpConfig(
                server_host=["127.0.0.1"],
                server_port=_BACKEND_PORT,
                cors_allowed_origins=["https://cast.home-assistant.io"],
                use_x_forwarded_for=True,
                trusted_proxies=["127.0.0.1/32"],
                login_attempts_threshold=-1,
                ip_ban_enabled=True,
                ssl_profile="modern",
                use_x_frame_options=True,
            ).model_dump(),
        },
        supplied=(("owner_password",),),
    )
    k8s.KubeJob(
        scope,
        "onboarding",
        metadata=k8s.ObjectMeta(
            name=_ONBOARDING, namespace=_NAMESPACE, annotations={"kustomize.toolkit.fluxcd.io/force": "enabled"}
        ),
        spec=k8s.JobSpec(
            backoff_limit=3,
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(
                    # Bump when bootstrap behavior changes so Flux replaces the immutable Job.
                    annotations={"home-assistant.allegedly.works/bootstrap-revision": "4"},
                    labels={"app.kubernetes.io/name": _ONBOARDING},
                ),
                spec=k8s.PodSpec(
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    restart_policy="OnFailure",
                    containers=[
                        k8s.Container(
                            name="onboarding",
                            image=_ONBOARDING_IMAGE,
                            image_pull_policy="Always",
                            command=["/homeassistant/provisioner/onboarding/onboard_bin"],
                            env=[
                                _settings_env(onboarding.Settings),
                                k8s.EnvVar(
                                    name=env_name(onboarding.Settings, "owner_password"),
                                    value_from=_BREAK_GLASS_PASSWORD,
                                ),
                            ],
                            resources=k8s.ResourceRequirements(
                                requests=_quantities(cpu="20m", memory="64Mi"), limits=_quantities(memory="256Mi")
                            ),
                            volume_mounts=[_settings_mount("settings")],
                        )
                    ],
                    volumes=[_config_map_volume("settings", settings.name)],
                ),
            ),
        ),
    )


def _token_provisioner(scope: Construct) -> None:
    """The identity that writes the token Secrets, and the CronJob that keeps them valid: it mints
    each token that is missing, the first ones after a bootstrap included, and replaces one Home
    Assistant refuses after expiry, revocation or a restore."""
    k8s.KubeServiceAccount(
        scope, "token-provisioner", metadata=k8s.ObjectMeta(name=_TOKEN_PROVISIONER, namespace=_NAMESPACE)
    )
    k8s.KubeRole(
        scope,
        "token-provisioner-role",
        metadata=k8s.ObjectMeta(name=_TOKEN_PROVISIONER, namespace=_NAMESPACE),
        rules=[
            k8s.PolicyRule(
                api_groups=[""],
                resources=["secrets"],
                resource_names=[token.secret_name for token in _TOKENS],
                verbs=["get", "update", "patch"],
            ),
            k8s.PolicyRule(api_groups=[""], resources=["secrets"], verbs=["create"]),
        ],
    )
    settings = _settings_config_map(
        scope,
        _TOKEN_PROVISIONER,
        tokens.Settings,
        {
            "endpoint": _ENDPOINT.model_dump(),
            "owner_username": _OWNER_USERNAME,
            "tokens": [token.model_dump(exclude_defaults=True) for token in _TOKENS],
        },
        supplied=(("owner_password",),),
    )
    k8s.KubeRoleBinding(
        scope,
        "token-provisioner-binding",
        metadata=k8s.ObjectMeta(name=_TOKEN_PROVISIONER, namespace=_NAMESPACE),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=_TOKEN_PROVISIONER),
        subjects=[k8s.Subject(kind="ServiceAccount", name=_TOKEN_PROVISIONER, namespace=_NAMESPACE)],
    )
    labels = {"app.kubernetes.io/name": _TOKEN_PROVISIONER}
    k8s.KubeCronJob(
        scope,
        "token-provisioner-cronjob",
        metadata=k8s.ObjectMeta(name=_TOKEN_PROVISIONER, namespace=_NAMESPACE, labels=labels),
        spec=k8s.CronJobSpec(
            # A valid token costs one request, so the interval is what bounds how long a missing or
            # refused one lasts.
            schedule="*/15 * * * *",
            concurrency_policy="Forbid",
            job_template=k8s.JobTemplateSpec(
                spec=k8s.JobSpec(
                    # Ends a run that never starts, such as one still pulling an image Flux has not
                    # pinned yet; under `Forbid` it would otherwise hold off every later run.
                    active_deadline_seconds=600,
                    backoff_limit=3,
                    template=k8s.PodTemplateSpec(
                        metadata=k8s.ObjectMeta(labels=labels),
                        spec=k8s.PodSpec(
                            image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                            restart_policy="OnFailure",
                            service_account_name=_TOKEN_PROVISIONER,
                            automount_service_account_token=True,
                            security_context=k8s.PodSecurityContext(
                                run_as_non_root=True, seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault")
                            ),
                            containers=[
                                k8s.Container(
                                    name="provision-tokens",
                                    image=_TOKEN_PROVISIONER_IMAGE,
                                    image_pull_policy="Always",
                                    command=["/homeassistant/provisioner/tokens/provision_bin"],
                                    env=[
                                        _settings_env(tokens.Settings),
                                        k8s.EnvVar(
                                            name=env_name(tokens.Settings, "owner_password"),
                                            value_from=_BREAK_GLASS_PASSWORD,
                                        ),
                                    ],
                                    resources=k8s.ResourceRequirements(
                                        requests=_quantities(cpu="20m", memory="64Mi"),
                                        limits=_quantities(memory="256Mi"),
                                    ),
                                    security_context=k8s.SecurityContext(
                                        allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"])
                                    ),
                                    volume_mounts=[_settings_mount("settings")],
                                )
                            ],
                            volumes=[_config_map_volume("settings", settings.name)],
                        ),
                    ),
                )
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
    add_external_secret(
        scope,
        "metrics-token",
        name=_METRICS_TOKEN,
        namespace=_NAMESPACE,
        refresh=ExternalSecretSpecRefreshPolicy.CREATED_ONCE,
        data_from=[password_generator(generator.name)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
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
    _token_provisioner(chart)
    https_route(
        chart,
        "route",
        metadata=metadata(_NAME, _NAMESPACE),
        hostname=_HOSTNAME,
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
