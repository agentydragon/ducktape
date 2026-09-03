"""Run the Agentplane egress proxy over one namespace's policy."""

from __future__ import annotations

import asyncio
import logging
import signal
from ipaddress import IPv4Network, IPv6Network
from pathlib import Path
from typing import Any, cast

from kubernetes_asyncio import client as k8s_client, config as k8s_config
from kubernetes_asyncio.client import ApiClient, AuthenticationV1Api, CoreV1Api, CustomObjectsApi
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from util.kubernetes import CustomObjectsClient
from x.agentplane.egress.addon import EgressAddon
from x.agentplane.egress.admin import create_admin_app, serve_admin
from x.agentplane.egress.decisions import DecisionRing
from x.agentplane.egress.identity import PodIdentityVerifier
from x.agentplane.egress.informer import Informer
from x.agentplane.egress.policy import Index
from x.agentplane.egress.proxy import EgressProxyServer, write_interception_ca
from x.agentplane.egress.upstream import UpstreamResolver

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Each field is a `--flag` and an `AGENTPLANE_EGRESS_*` environment variable."""

    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_EGRESS_", cli_parse_args=True, cli_kebab_case=True)

    namespace: str = Field(description="Namespace holding the policies, bindings, and Sandboxes.")
    credentials_namespace: str = Field(
        default="agentplane-egress-credentials", description="Namespace the rules' Secrets are read from."
    )
    listen_host: str = Field(default="0.0.0.0", description="Proxy listener bind address.")
    listen_port: int = Field(default=8888, description="Proxy listener port the sidecars relay to.")
    admin_host: str = Field(default="0.0.0.0", description="Admin listener bind address.")
    admin_port: int = Field(default=8081, description="Admin port serving /decisions and /healthz.")
    ca_cert: Path = Field(description="PEM certificate of the interception CA the runner containers trust.")
    ca_key: Path = Field(description="PEM private key of the interception CA.")
    confdir: Path = Field(description="Writable directory mitmproxy keeps its CA and issued leaves in.")
    token_audience: str = Field(default="agentplane-egress", description="Audience of the sidecars' projected tokens.")
    kubeconfig: Path | None = Field(default=None, description="Kubeconfig to use; omit for in-cluster.")
    resync_seconds: float = Field(default=300, description="Watch lifetime; every kind is relisted this often.")
    identity_cache_seconds: float = Field(default=60, description="Upper bound on how long a token verdict is kept.")
    decision_ring_size: int = Field(default=200, description="Decisions kept per sandbox for /decisions.")
    exempt_networks: list[IPv4Network | IPv6Network] = Field(
        default_factory=list,
        description="Networks an admitted host may resolve into although they are not globally reachable.",
    )

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
        ring = DecisionRing(settings.decision_ring_size)
        # Cast so `patch_namespaced_custom_object_status` accepts `_content_type` (see util.kubernetes).
        custom_objects = cast(CustomObjectsClient, CustomObjectsApi(api))
        informer = Informer(
            index=index,
            custom_objects=custom_objects,
            core_v1=CoreV1Api(api),
            namespace=settings.namespace,
            credentials_namespace=settings.credentials_namespace,
            resync_seconds=settings.resync_seconds,
        )
        verifier = PodIdentityVerifier(
            authentication=AuthenticationV1Api(api),
            core_v1=CoreV1Api(api),
            namespace=settings.namespace,
            audience=settings.token_audience,
            cache_seconds=settings.identity_cache_seconds,
        )
        resolver = UpstreamResolver(exempt=frozenset(settings.exempt_networks))
        addon = EgressAddon(index=index, verifier=verifier, ring=ring, resolver=resolver)
        informer_task = asyncio.create_task(informer.run(), name="egress-informer")
        try:
            async with (
                serve_admin(create_admin_app(ring, index), settings.admin_host, settings.admin_port) as admin_port,
                EgressProxyServer(
                    addon, confdir=settings.confdir, listen_host=settings.listen_host, listen_port=settings.listen_port
                ),
            ):
                logger.info("admin listening on %s:%d", settings.admin_host, admin_port)
                await stop.wait()
        finally:
            informer_task.cancel()
            await asyncio.gather(informer_task, return_exceptions=True)


if __name__ == "__main__":
    main()
