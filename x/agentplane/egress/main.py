"""Run the Agentplane egress proxy: policy from one namespace, over sandboxes in another."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import timedelta
from ipaddress import IPv4Network, IPv6Network
from pathlib import Path
from typing import Any, cast

from kubernetes_asyncio import client as k8s_client, config as k8s_config
from kubernetes_asyncio.client import ApiClient, AuthenticationV1Api, CoreV1Api, CustomObjectsApi
from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

from util.kubernetes import CustomObjectsClient
from x.agentplane.egress.addon import EgressAddon
from x.agentplane.egress.admin import create_admin_app, serve_admin
from x.agentplane.egress.decision_log import DecisionLog
from x.agentplane.egress.decision_store import DecisionStore, make_engine
from x.agentplane.egress.identity import ProjectedTokenVerifier, WorkloadIdentityVerifier
from x.agentplane.egress.informer import Informer
from x.agentplane.egress.policy import Index
from x.agentplane.egress.proxy import EgressProxyServer, write_interception_ca
from x.agentplane.egress.rules_api import RulesProjection, create_rules_app, serve_rules_api
from x.agentplane.egress.upstream import UpstreamResolver
from x.agentplane.kubernetes_watch import STALE_AFTER_CYCLES
from x.agentplane.workload_auth.http import WorkloadPrincipalAuthenticator
from x.agentplane.workload_auth.principal import WorkloadPrincipalResolver

# YamlConfigSettingsSource loads yaml lazily inside pydantic-settings; gazelle cannot see the dependency.
# gazelle:include_dep @pypi//pyyaml

logger = logging.getLogger(__name__)


# Names the YAML settings file a deployment mounts; not a field, so not a flag.
CONFIG_FILE_ENV = "AGENTPLANE_EGRESS_CONFIG_FILE"


class Settings(BaseSettings):
    """Each field is a `--flag` and an `AGENTPLANE_EGRESS_*` environment variable."""

    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_EGRESS_", cli_parse_args=True, cli_kebab_case=True)

    rules_namespace: str = Field(
        description="The one namespace holding the EgressPolicy, EgressBinding and EgressCredential objects this "
        "proxy enforces. One deployment serves one policy set; a caller's own namespace is unrelated to it."
    )
    credentials_namespace: str = Field(
        default="agentplane-egress-credentials", description="Namespace the rules' Secrets are read from."
    )
    allowed_service_account_namespaces: frozenset[str] = Field(
        min_length=1,
        description="Every namespace whose ServiceAccounts may authenticate here, the sandbox namespace included. "
        "An agent this cluster does not host runs where it runs, so naming its namespace is what lets it present a "
        "token at all; it grants nothing on its own, since a subject no binding names still reaches no rule.",
    )
    listen_host: str = Field(default="0.0.0.0", description="Proxy listener bind address.")
    listen_port: int = Field(default=8888, description="Proxy listener port the sidecars relay to.")
    admin_host: str = Field(default="0.0.0.0", description="Admin listener bind address.")
    admin_port: int = Field(default=8081, description="Admin port serving /decisions and /healthz.")
    agent_api_host: str = Field(default="0.0.0.0", description="Agent-facing rules API bind address.")
    agent_api_port: int = Field(default=8082, description="Agent-facing HTTP port serving /v1/rules.")
    ca_cert: Path = Field(description="PEM certificate of the interception CA the runner containers trust.")
    ca_key: Path = Field(description="PEM private key of the interception CA.")
    confdir: Path = Field(description="Writable directory mitmproxy keeps its CA and issued leaves in.")
    token_audience: str = Field(default="agentplane-egress", description="Audience of the sidecars' projected tokens.")
    projected_token_audiences: frozenset[str] = Field(
        default=frozenset(),
        description="Every audience a rule may substitute a sidecar's projected token for, the API server's "
        "among them. Reviewing the list here is what decides which destinations this proxy will spend a "
        "caller's own identity on; a credential naming an audience absent from it resolves to nothing.",
    )
    kubeconfig: Path | None = Field(default=None, description="Kubeconfig to use; omit for in-cluster.")

    resync_seconds: int = Field(default=300, gt=0, description="Watch lifetime; every kind is relisted this often.")
    database_url: str = Field(repr=False, description="Shared diagnostic PostgreSQL database; migrated separately.")
    decision_history_size: int = Field(default=200, ge=1, le=1000)
    decision_retention_days: int = Field(default=7, ge=1, le=365)
    decision_queue_size: int = Field(default=2000, ge=1, le=100000)
    decision_batch_size: int = Field(default=100, ge=1, le=1000)
    decision_flush_seconds: float = Field(default=5, gt=0, le=20)
    exempt_networks: list[IPv4Network | IPv6Network] = Field(
        default_factory=list,
        description="Networks an admitted host may resolve into although they are not globally reachable.",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        if config_file := os.environ.get(CONFIG_FILE_ENV):
            # pydantic-settings silently ignores absent YAML files. An explicit deployment binding
            # must never turn into a healthy service running on defaults.
            if not Path(config_file).is_file():
                raise ValueError("configured egress proxy settings file is not a regular file")
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=config_file))
        sources.append(file_secret_settings)
        return tuple(sources)

    def __init__(self, **values: Any) -> None:
        # BaseSettings fills required fields from its sources; spell that out because the mypy plugin
        # derives a required-argument signature from the fields.
        super().__init__(**values)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(async_main(Settings()))


async def async_main(settings: Settings) -> None:
    configuration = k8s_client.Configuration()
    if settings.kubeconfig is None:
        k8s_config.load_incluster_config(client_configuration=configuration)
    else:
        await k8s_config.load_kube_config(config_file=str(settings.kubeconfig), client_configuration=configuration)
    write_interception_ca(settings.confdir, settings.ca_cert, settings.ca_key)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    async with ApiClient(configuration=configuration) as api:
        index = Index()
        decision_log = DecisionLog(
            DecisionStore(
                make_engine(settings.database_url),
                retention=timedelta(days=settings.decision_retention_days),
                capacity=settings.decision_history_size,
            ),
            queue_size=settings.decision_queue_size,
            batch_size=settings.decision_batch_size,
        )
        decision_log.start()
        custom_objects = cast(CustomObjectsClient, CustomObjectsApi(api))
        informer = Informer(
            index=index,
            custom_objects=custom_objects,
            core_v1=CoreV1Api(api),
            namespace=settings.rules_namespace,
            credentials_namespace=settings.credentials_namespace,
            resync_seconds=settings.resync_seconds,
        )
        workload_resolver = WorkloadPrincipalResolver(
            authentication=AuthenticationV1Api(api),
            audience=settings.token_audience,
            allowed_service_account_namespaces=settings.allowed_service_account_namespaces,
        )
        # One resolver per audience, differing only in the audience each reviews for: a projected
        # token proves the same Pod as the hop bearer or it is not substituted.
        projected = ProjectedTokenVerifier(
            resolvers={
                audience: WorkloadPrincipalResolver(
                    authentication=AuthenticationV1Api(api),
                    audience=audience,
                    allowed_service_account_namespaces=settings.allowed_service_account_namespaces,
                )
                for audience in settings.projected_token_audiences
            }
        )
        resolver = UpstreamResolver(exempt=frozenset(settings.exempt_networks))
        addon = EgressAddon(
            index=index,
            verifier=WorkloadIdentityVerifier(workload_resolver),
            decision_log=decision_log,
            resolver=resolver,
            stale_after_seconds=settings.resync_seconds * STALE_AFTER_CYCLES,
            projected=projected,
        )
        # One resolver for both doors: the tunnel and the rules API authenticate the same bearers,
        # so a verdict either reached is a verdict the other need not spend a TokenReview on.
        rules_app = create_rules_app(WorkloadPrincipalAuthenticator(workload_resolver), RulesProjection(index))
        informer_task = asyncio.create_task(informer.run(), name="egress-informer")
        try:
            async with (
                serve_admin(
                    create_admin_app(decision_log, index, resync_seconds=settings.resync_seconds),
                    settings.admin_host,
                    settings.admin_port,
                ) as admin_port,
                serve_rules_api(rules_app, settings.agent_api_host, settings.agent_api_port),
                EgressProxyServer(
                    addon, confdir=settings.confdir, listen_host=settings.listen_host, listen_port=settings.listen_port
                ),
            ):
                logger.info("admin listening on %s:%d", settings.admin_host, admin_port)
                logger.info("agent API listening on %s:%d", settings.agent_api_host, settings.agent_api_port)
                await stop.wait()
        finally:
            informer_task.cancel()
            await asyncio.gather(informer_task, return_exceptions=True)
            await decision_log.close(settings.decision_flush_seconds)


if __name__ == "__main__":
    main()
