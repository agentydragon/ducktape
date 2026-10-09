"""Run the Agentplane egress proxy: egress policies from one namespace, over sandboxes in another."""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import timedelta
from typing import cast

from kubernetes_asyncio import client as k8s_client, config as k8s_config
from kubernetes_asyncio.client import ApiClient, AuthenticationV1Api, CoreV1Api, CustomObjectsApi

from agentplane.egress.addon import EgressAddon
from agentplane.egress.admin import create_admin_app, serve_admin
from agentplane.egress.decision_log import DecisionLog
from agentplane.egress.decision_store import DecisionStore, make_engine
from agentplane.egress.identity import ProjectedTokenVerifier, WorkloadIdentityVerifier
from agentplane.egress.informer import Informer
from agentplane.egress.policy import Index
from agentplane.egress.proxy import EgressProxyServer, write_interception_ca
from agentplane.egress.rules_api import RulesProjection, create_rules_app, serve_rules_api
from agentplane.egress.settings import Settings
from agentplane.egress.upstream import PinnedDialEventLoop, UpstreamResolver
from agentplane.kubernetes_watch import STALE_AFTER_CYCLES
from agentplane.workload_auth.http import WorkloadPrincipalAuthenticator
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.kubernetes import CustomObjectsClient

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(async_main(Settings()), loop_factory=PinnedDialEventLoop)


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
        resolver = UpstreamResolver(exempt=settings.exempt_networks)
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
                EgressProxyServer(
                    addon,
                    confdir=settings.confdir,
                    upstream_ca_file=settings.upstream_ca_file,
                    listen_host=settings.listen_host,
                    listen_port=settings.listen_port,
                ),
                serve_rules_api(rules_app, settings.agent_api_host, settings.agent_api_port),
                # Admitted last, so that /healthz answering at all means the two listeners above
                # are bound. It is the readiness probe, and readiness gates every Service this Pod
                # backs -- entered first it reports the Pod ready while mitmproxy, much the slower
                # of the two to bind, still refuses connections on the tunnel port.
                serve_admin(
                    create_admin_app(decision_log, index, resync_seconds=settings.resync_seconds),
                    settings.admin_host,
                    settings.admin_port,
                ) as admin_port,
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
