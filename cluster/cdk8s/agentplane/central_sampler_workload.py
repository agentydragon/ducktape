"""The optional Central Sampler Deployment and its cross-service policy."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import ContainerResources, Cpu, CpuResources, EnvValue, MemoryResources, PathMapping, Secret, Volume
from constructs import Construct

from agentplane.net.peertls.config import (
    CENTRAL_SAMPLER_URI,
    GATEWAY_SESSIONS_URI,
    HISTORY_SERVICE_URI,
    SANDBOX_CONTROLLER_URI,
    ServerTLSListener,
)
from agentplane.sampler.settings import Settings
from agentplane.session_instructions.platform_instructions import SessionRuntime
from cluster.cdk8s import cilium, node_scheduling, public_web_pki
from cluster.cdk8s.agentplane import (
    actions,
    central_sampler,
    database,
    environment_service,
    history_service,
    llm_ingress,
    workload_tls,
)
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.platform_instructions import platform_instructions
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.config_format import json_config
from cluster.cdk8s.probes import grpc_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.service_ref import Port
from cluster.cdk8s.settings_file import SettingsFile
from util.settings_contract import env_name

_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-central-sampler:unset"
_CONFIG_PATH = "/etc/agentplane-central-sampler/settings.json"
_LOGS_PATH = "/var/log/central-sampler"


class CentralSampler(Construct):
    """Sampler replicas each host Sessions selected through the database lease."""

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
        self.history_ingress = history_service.service(namespace)

        settings = Settings(
            environment_namespace=namespace,
            health_listen=f"0.0.0.0:{central_sampler.HEALTH_PORT}",
            listen_tls=ServerTLSListener(
                listen=f"0.0.0.0:{self.service.pod_port}",
                cert_file=Path(workload_tls.CERT_FILE),
                key_file=Path(workload_tls.KEY_FILE),
                client_ca_file=Path(workload_tls.TRUST_BUNDLE_FILE),
            ),
            connections=dict(props.connections),
            actions=props.actions,
            models=dict(props.models),
            platform_instructions=platform_instructions(env, session_runtime=SessionRuntime.CENTRAL_SAMPLER),
            service_callers=[GATEWAY_SESSIONS_URI],
            admin_service_callers=list(props.admin_service_callers),
            code_mode_host=Path("/usr/local/bin/codex-code-mode-host"),
            # The sandbox exec-server resolves this binary through its normal PATH.
            executor_codex="codex",
            logs=Path(_LOGS_PATH),
            history_target=self.history_ingress.authority,
            history_tls=workload_tls.service_client_tls(HISTORY_SERVICE_URI, client_uri=CENTRAL_SAMPLER_URI),
            gateway_target=environment_service.environments_control_target(namespace),
            gateway_tls=workload_tls.service_client_tls(SANDBOX_CONTROLLER_URI, client_uri=CENTRAL_SAMPLER_URI),
            max_live_sessions=props.max_live_sessions,
            rpc_timeout_seconds=props.rpc_timeout_seconds,
            executor_initialize_timeout_seconds=props.executor_initialize_timeout_seconds,
            lease_seconds=props.lease_seconds,
            turn_timeout_seconds=props.turn_timeout_seconds,
            auto_compact_tokens=props.auto_compact_tokens,
        )
        settings_file = SettingsFile(
            self,
            "settings",
            metadata=ApiObjectMetadata(name=f"{central_sampler.NAME}-settings", namespace=namespace),
            content=json_config(settings.model_dump(mode="json", exclude_none=True)),
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
            extra_ports=[Port(name="health", number=central_sampler.HEALTH_PORT).container_port()],
            env_variables={
                config_env: EnvValue.from_value(_CONFIG_PATH),
                database_env: database_url,
                "SSL_CERT_FILE": EnvValue.from_value(public_web_pki.CA_FILE),
            },
            readiness=grpc_probe(port=central_sampler.HEALTH_PORT, initial_delay_seconds=3, period_seconds=10),
            liveness=grpc_probe(port=central_sampler.HEALTH_PORT, initial_delay_seconds=20, period_seconds=30),
            startup=grpc_probe(
                port=central_sampler.HEALTH_PORT, initial_delay_seconds=0, period_seconds=5, failure_threshold=60
            ),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(100), limit=Cpu.millis(2000)),
                memory=MemoryResources(request=Size.mebibytes(512), limit=Size.gibibytes(4)),
            ),
            termination_grace=Duration.seconds(60),
            # Provider Secret files are reread per request; rotating them must not restart the shared host.
            replica_profile=props.replicas or env.replicas,
            placement=placement,
            registry_secret_name=registry_secret_name,
        )
        workload.container.mount(str(Path(_CONFIG_PATH).parent), settings_volume, read_only=True)
        workload.container.mount(_LOGS_PATH, Volume.from_empty_dir(self, "logs-volume", "logs"))
        workload_tls.service_certificate(
            self, env, container=workload.container, name=central_sampler.NAME, uri=CENTRAL_SAMPLER_URI
        )
        public_web_pki.mount(self, workload.container)
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

    def add_platform_resources(self) -> None:
        """Add the API admission and egress rules used by the sampler runtime."""
        namespace = self.env.namespace
        props = self.env.central_sampler
        assert props is not None
        egress = [
            cilium.dns_egress(),
            EgressRule.to_entities(Entity.KUBE_APISERVER),
            database.egress(namespace),
            environment_service.environments_service(namespace).egress(),
            history_service.service(namespace).egress(),
            llm_ingress.service(namespace).egress(),
        ]
        if props.actions is not None:
            egress.append(actions.service(namespace).egress())
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
            egress=egress,
        )
