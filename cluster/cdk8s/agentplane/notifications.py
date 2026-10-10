"""Notification-owned persistence and workload API; only Sandbox Service reaches runners."""

from cdk8s import ApiObject, ApiObjectMetadata, Duration, JsonPatch, Size
from cdk8s_plus_34 import (
    ContainerResources,
    ContainerSecurityContextProps,
    Cpu,
    CpuResources,
    Deployment,
    ImagePullPolicy,
    MemoryResources,
    PodSecurityContextProps,
    Service,
    ServiceAccount,
    k8s,
)
from constructs import Construct

from agentplane.notification_service.settings import CONFIG_FILE_ENV, TOKEN_AUDIENCE, Settings
from cluster.cdk8s import cilium, node_scheduling, pod_policy
from cluster.cdk8s.agentplane import database
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.pod_disruption_budget import add_pod_disruption_budget
from cluster.cdk8s.forgejo_registry.chart import forgejo_images_creds_secret_ref
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, IngressRule, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import env_name

NAME = "agentplane-notifications"
WORKLOAD_CREDENTIAL = "agentplane-notifications-workload"
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-notification-service"
_LABELS = {"app.kubernetes.io/name": NAME}


def service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME, port=Port(name="http", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


class Notifications(Construct):
    def __init__(
        self, scope: Construct, id: str, env: Environment, *, actions: ServiceRef, sandboxes: ServiceRef
    ) -> None:
        super().__init__(scope, id)
        endpoint = service(env.namespace)
        account = ServiceAccount(
            self, "account", metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace), automount_token=True
        )
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{env.namespace}-notifications-token-reviewer",
            service_account_name=NAME,
            namespace=env.namespace,
        )
        variables = {
            "AGENTPLANE_NOTIFICATIONS_DATABASE_URL": SecretRef(namespace=env.namespace, name="postgres-notifications")
            .key("uri")
            .env_value(self, "database")
        }
        deployment = Deployment(
            self,
            "deployment",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace, labels=_LABELS),
            pod_metadata=ApiObjectMetadata(labels=_LABELS),
            replicas=env.replicas.count,
            strategy=env.replicas.strategy,
            min_ready=env.replicas.min_ready,
            termination_grace_period=Duration.seconds(30),
            service_account=account,
            automount_service_account_token=True,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "images-creds"),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000, fs_group=1000),
            init_containers=[
                migrate_init_container(f"{_IMAGE}-migrate:unset", name="migrate", env_variables=variables)
            ],
        )
        github = env.notifications_github
        supplied: list[tuple[str, ...]] = [("database_url",)]
        if github is not None:
            supplied.extend([("github", "private_key"), ("github", "webhook_secret")])
        settings = SettingsFile(
            self,
            "settings",
            metadata=ApiObjectMetadata(name=f"{NAME}-settings", namespace=env.namespace),
            model=Settings,
            path="/etc/agentplane-notifications/settings.yaml",
            content={
                "namespace": env.namespace,
                "token_audience": TOKEN_AUDIENCE,
                "operator_reader_account": "agentplane-app",
                "notice_debounce": {"quiet_seconds": 60, "max_wait_seconds": 120},
                "actions": {
                    "url": f"http://{actions.fqdn}:{actions.port.number}",
                    "token_file": "/var/run/secrets/notifications/actions",
                },
                "sandbox_service": {
                    "target": f"{sandboxes.fqdn}:{sandboxes.port.number}",
                    "grpc_channel_options": env.app_config.sandbox_service_grpc_channel_options,
                    "token_file": "/var/run/secrets/notifications/sandboxes",
                },
                "github": {"app_id": github.app_id} if github is not None else None,
            },
            supplied=supplied,
        )
        container = deployment.add_container(
            name="notifications",
            image=f"{_IMAGE}:unset",
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            env_variables=variables,
            ports=[endpoint.port.container_port()],
            readiness=http_probe("/readyz", port=8080, initial_delay_seconds=3, period_seconds=10),
            liveness=http_probe("/healthz", port=8080, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            security_context=ContainerSecurityContextProps(read_only_root_filesystem=False),
        )
        if github is not None:
            secret = SecretRef(namespace=env.namespace, name=github.secret_name)
            for field, key in [("private_key", "private-key"), ("webhook_secret", "webhook-secret")]:
                container.env.add_variable(
                    env_name(Settings, "github", field), secret.key(key).env_value(self, f"github-{key}")
                )
        settings.mount_into(container, env=CONFIG_FILE_ENV)
        ApiObject.of(deployment).add_json_patch(
            JsonPatch.add(
                "/spec/template/spec/volumes/-",
                k8s.Volume(
                    name="service-tokens",
                    projected=k8s.ProjectedVolumeSource(
                        sources=[
                            k8s.VolumeProjection(
                                service_account_token=k8s.ServiceAccountTokenProjection(
                                    audience=audience, expiration_seconds=3600, path=path
                                )
                            )
                            for path, audience in [
                                ("actions", "agentplane-egress"),
                                ("sandboxes", "agentplane-sandbox-service"),
                            ]
                        ]
                    ),
                ),
            )
        )
        ApiObject.of(deployment).add_json_patch(
            JsonPatch.add(
                "/spec/template/spec/containers/0/volumeMounts/-",
                k8s.VolumeMount(name="service-tokens", mount_path="/var/run/secrets/notifications", read_only=True),
            )
        )
        pod_policy.place(deployment, node_scheduling.HIL_OVH)
        pod_policy.harden(deployment)
        Service(
            self,
            "service",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace, labels=_LABELS),
            selector=deployment,
            ports=[endpoint.port.service_port()],
        )
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace),
            endpoint_selector=_LABELS,
            ingress=[
                IngressRule.from_endpoints(cilium.endpoint_labels(env.namespace, "agentplane-egress"), ports=[8080]),
                IngressRule.from_endpoints(cilium.endpoint_labels(env.namespace, "agentplane-app"), ports=[8080]),
            ],
            egress=[
                cilium.dns_egress(resolves=["*"]),
                EgressRule.to_fqdns("api.github.com"),
                EgressRule.to_entities(Entity.KUBE_APISERVER),
                actions.egress(),
                sandboxes.egress(),
                EgressRule.to_endpoints(
                    {"k8s:io.kubernetes.pod.namespace": env.namespace, "k8s:cnpg.io/cluster": "postgres"},
                    database.POSTGRES_PORT,
                ),
            ],
        )
        if env.replicas.pdb_min_available is not None:
            add_pod_disruption_budget(
                self,
                "pdb",
                name=NAME,
                namespace=env.namespace,
                min_available=env.replicas.pdb_min_available,
                selector=_LABELS,
            )
