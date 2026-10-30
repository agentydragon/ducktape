"""The optional Central Sampler Deployment and its cross-service policy."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ContainerProps,
    ContainerResources,
    Cpu,
    CpuResources,
    DeploymentStrategy,
    EnvValue,
    ImagePullPolicy,
    MemoryResources,
    PathMapping,
    Secret,
    Volume,
)
from constructs import Construct

from agentplane.workload_auth.audience import WORKLOAD_TOKEN_AUDIENCE
from agentplane.x.central_sampler.settings import Settings
from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.agentplane import central_sampler, database, history_service, llm_ingress, runner_gateway
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.config_format import json_config
from cluster.cdk8s.probes import grpc_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.settings_file import SettingsFile
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import env_name

_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-central-sampler:unset"
_CONFIG_PATH = "/etc/agentplane-central-sampler/settings.json"
_GATEWAY_TOKEN_FILE = Path("/var/run/secrets/central-sampler/gateway")
_HISTORY_TOKEN_FILE = Path("/var/run/secrets/central-sampler/history")
_LOGS_PATH = "/var/log/central-sampler"


class CentralSampler(Construct):
    """A single sampler replica owns one shared host for concurrent logical Sessions."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        database_url: EnvValue,
        placement: node_scheduling.Placement | None = node_scheduling.HIL_OVH,
        registry_secret_name: str | None = "forgejo-images-creds",
    ) -> None:
        super().__init__(scope, id)
        props = env.central_sampler
        if props is None:
            raise ValueError("CentralSampler requires Environment.central_sampler")

        namespace = env.namespace
        self.env = env
        self.service = central_sampler.service(namespace)
        self.llm_ingress = llm_ingress.service(namespace)
        self.history_ingress = history_service.ingress_service(namespace)
        self.runner_gateway = runner_gateway.service(namespace)

        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{namespace}-central-sampler-token-reviewer",
            service_account_name=central_sampler.NAME,
            namespace=namespace,
        )

        settings = Settings(
            listen=f"0.0.0.0:{self.service.pod_port}",
            connections=dict(props.connections),
            models=dict(props.models),
            audience=central_sampler.TOKEN_AUDIENCE,
            allowed_accounts={f"{namespace}/{runner_gateway.NAME}"},
            code_mode_host=Path("/usr/local/bin/codex-code-mode-host"),
            # The sandbox exec-server resolves this binary through its normal PATH.
            executor_codex="codex",
            logs=Path(_LOGS_PATH),
            history_target=f"{self.history_ingress.fqdn}:{self.history_ingress.pod_port}",
            history_token_file=_HISTORY_TOKEN_FILE,
            gateway_target=runner_gateway.control_target(namespace),
            gateway_token_file=_GATEWAY_TOKEN_FILE,
            max_live_sessions=props.max_live_sessions,
            turn_timeout_seconds=props.turn_timeout_seconds,
            rpc_timeout_seconds=props.rpc_timeout_seconds,
            executor_initialize_timeout_seconds=props.executor_initialize_timeout_seconds,
            lease_seconds=props.lease_seconds,
            auto_compact_tokens=props.auto_compact_tokens,
        )
        settings_file = SettingsFile(
            self,
            "settings",
            metadata=ApiObjectMetadata(name=f"{central_sampler.NAME}-settings", namespace=namespace),
            content=json_config(settings),
            path=_CONFIG_PATH,
        )
        settings_volume = Volume.from_config_map(
            self, "settings-volume", settings_file.config_map, name=f"{central_sampler.NAME}-settings"
        )
        config_env = Settings.config_file_env
        database_env = env_name(Settings, "database_url")
        workload = add_service_workload(
            self,
            env,
            serves=[self.service],
            container=central_sampler.NAME,
            image=_IMAGE,
            env_variables={config_env: EnvValue.from_value(_CONFIG_PATH), database_env: database_url},
            readiness=grpc_probe(port=self.service.pod_port, initial_delay_seconds=3, period_seconds=10),
            liveness=grpc_probe(port=self.service.pod_port, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(100), limit=Cpu.millis(2000)),
                memory=MemoryResources(request=Size.mebibytes(512), limit=Size.gibibytes(4)),
            ),
            termination_grace=Duration.seconds(60),
            annotations={
                "configmap.reloader.stakater.com/reload": settings_file.config_map.name,
                **(
                    {
                        "secret.reloader.stakater.com/reload": ",".join(
                            sorted({key.secret.name for key in props.connection_credentials.values()})
                        )
                    }
                    if props.connection_credentials
                    else {}
                ),
            },
            replica_profile=replace(
                env.replicas, count=1, strategy=DeploymentStrategy.recreate(), min_ready=None, pdb_min_available=None
            ),
            migrate=ContainerProps(
                name="initialize-database",
                image=_IMAGE,
                image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
                args=["--initialize"],
                env_variables={config_env: EnvValue.from_value(_CONFIG_PATH), database_env: database_url},
                resources=ContainerResources(
                    cpu=CpuResources(request=Cpu.millis(25)),
                    memory=MemoryResources(request=Size.mebibytes(64), limit=Size.mebibytes(256)),
                ),
            ),
            placement=placement,
            registry_secret_name=registry_secret_name,
        )
        workload.container.mount(str(Path(_CONFIG_PATH).parent), settings_volume, read_only=True)
        workload.deployment.init_containers[0].mount(str(Path(_CONFIG_PATH).parent), settings_volume, read_only=True)
        workload.container.mount(_LOGS_PATH, Volume.from_empty_dir(self, "logs-volume", "logs"))
        for connection_name, secret_key in props.connection_credentials.items():
            connection = props.connections.get(connection_name)
            if connection is None:
                raise ValueError(f"credential configured for unknown connection {connection_name!r}")
            credential_file = connection.auth.credential_file
            if credential_file is None:
                raise ValueError(f"connection {connection_name!r} has a Secret but no credential_file")
            if secret_key.secret.namespace != namespace:
                raise ValueError(f"connection {connection_name!r} credential Secret must be in {namespace!r}")
            credential_volume = Volume.from_secret(
                self,
                f"{connection_name}-credential-volume",
                Secret.from_secret_name(self, f"{connection_name}-credential-secret", secret_key.secret.name),
                default_mode=0o440,
                items={secret_key.key: PathMapping(path=credential_file.name)},
            )
            workload.container.mount(str(credential_file.parent), credential_volume, read_only=True)
        workload.project_tokens(
            "caller-tokens",
            "/var/run/secrets/central-sampler",
            {
                "llm": WORKLOAD_TOKEN_AUDIENCE,
                "gateway": runner_gateway.TOKEN_AUDIENCE,
                "history": history_service.RUNNER_INGRESS_AUDIENCE,
            },
        )

    def add_platform_resources(self) -> None:
        """Add the API admission and egress rules used by the sampler runtime."""
        namespace = self.env.namespace
        props = self.env.central_sampler
        assert props is not None
        egress = [
            cilium.dns_egress(),
            EgressRule.to_entities(Entity.KUBE_APISERVER),
            database.egress(namespace),
            runner_gateway.service(namespace).egress(),
            history_service.ingress_service(namespace).egress(),
            llm_ingress.service(namespace).egress(),
        ]
        anthropic_connection = props.connections.get("anthropic-api")
        if (
            anthropic_connection is not None
            and urlsplit(str(anthropic_connection.base_url)).hostname == "api.anthropic.com"
        ):
            # The DNS proxy must observe a matching query before a Cilium toFQDNs
            # peer can authorize the resolved destination.
            egress.append(cilium.dns_egress(resolves=["api.anthropic.com"]))
            egress.append(EgressRule.to_fqdns("api.anthropic.com"))
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=central_sampler.NAME, namespace=namespace),
            endpoint_selector=self.service.pods.selector,
            ingress=[runner_gateway.service(namespace).pods.admit(self.service.pod_port)],
            egress=egress,
        )
