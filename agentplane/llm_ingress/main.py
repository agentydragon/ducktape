"""Run the Agentplane LLM workload ingress."""

from __future__ import annotations

import asyncio
import logging

import httpx
import uvicorn
from kubernetes_asyncio.client import ApiClient, AuthenticationV1Api

from agentplane.llm_ingress.app import IngressResources, create_app
from agentplane.llm_ingress.settings import Settings
from agentplane.service_process import configure_logging, kubernetes_configuration
from agentplane.workload_auth.http import WorkloadPrincipalAuthenticator
from agentplane.workload_auth.principal import WorkloadPrincipalResolver

logger = logging.getLogger(__name__)


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_levels)
    asyncio.run(async_main(settings))


async def async_main(settings: Settings) -> None:
    timeout = httpx.Timeout(connect=5, read=None, write=60, pool=5)
    async with (
        ApiClient(configuration=await kubernetes_configuration(settings.kubeconfig)) as api,
        httpx.AsyncClient(base_url=str(settings.litellm_url), timeout=timeout) as backend,
    ):
        resolver = WorkloadPrincipalResolver(
            authentication=AuthenticationV1Api(api),
            audience=settings.token_audience,
            allowed_service_account_namespaces=settings.allowed_service_account_namespaces,
        )
        app = create_app(
            IngressResources(
                authenticate=WorkloadPrincipalAuthenticator(resolver),
                backend=backend,
                litellm_key=settings.litellm_key.get_secret_value(),
                log_llm_requests=settings.log_llm_requests,
                models={item.model: item for item in settings.models},
            )
        )
        logger.info("forwarding authenticated Sandbox model traffic to %s", settings.litellm_url)
        await uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port, access_log=False)).serve()


if __name__ == "__main__":
    main()
