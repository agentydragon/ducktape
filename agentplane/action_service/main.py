"""Composition root for the independently deployable Agentplane Action Service."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from types import FrameType
from typing import cast

import httpx
import uvicorn
from kubernetes_asyncio.client import ApiClient, AuthenticationV1Api, CoreV1Api, CustomObjectsApi

from agentplane.action_service.api import create_app
from agentplane.action_service.auth import DisabledOperatorAuthenticator, OperatorAuthenticator
from agentplane.action_service.catalog import ActionCatalog
from agentplane.action_service.connections import ConnectionAuthority
from agentplane.action_service.db import ActionStore, make_engine, make_sessionmaker, verify_schema
from agentplane.action_service.enrollments import EnrollmentAuthority
from agentplane.action_service.github_policy.visibility import REQUEST_TIMEOUT_SECONDS, RepositoryVisibilityService
from agentplane.action_service.mcp_linkage import McpLinkageAuthority
from agentplane.action_service.oauth import running_oauth
from agentplane.action_service.operator_oidc import OidcOperatorAuthenticator
from agentplane.action_service.policy_evaluation import PolicySetDecisionProvider
from agentplane.action_service.policy_informer import PolicyIndex, PolicyInformer
from agentplane.action_service.push import ActionPushNotifier, PushIdentity, PushSubscriptionStore
from agentplane.action_service.runtime import running_executor
from agentplane.action_service.sandbox.inventory import SandboxClients
from agentplane.action_service.service import ActionService
from agentplane.action_service.settings import Settings
from agentplane.action_service.updates import ActionUpdateBroker
from agentplane.kubernetes_watch import STALE_AFTER_CYCLES, Freshness
from agentplane.service_process import background, configure_logging, kubernetes_configuration
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from mcp_infra.exec.kubernetes import KubernetesWebSocketExecRunner
from util.kubernetes import CustomObjectsClient

logger = logging.getLogger(__name__)


class ActionServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, service: ActionService) -> None:
        super().__init__(config)
        self._service = service

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        # Lifespan teardown happens AFTER HTTP/SSE shutdown. Fence admission and start
        # the execution deadline at signal receipt, not after that budget is spent.
        self._service.begin_drain()
        super().handle_exit(sig, frame)


def main() -> None:
    configure_logging()
    asyncio.run(async_main(Settings()))


async def async_main(settings: Settings) -> None:
    engine = make_engine(settings.database_url)
    async with AsyncExitStack() as stack:
        stack.push_async_callback(engine.dispose)
        await verify_schema(engine)
        configuration = await kubernetes_configuration(None)
        catalog = ActionCatalog(groups=settings.action_groups)
        visibility = RepositoryVisibilityService(
            await stack.enter_async_context(
                httpx.AsyncClient(base_url=settings.github_visibility.api_base_url, timeout=REQUEST_TIMEOUT_SECONDS)
            ),
            ttl_seconds=settings.github_visibility.cache_ttl_seconds,
        )
        api = await stack.enter_async_context(ApiClient(configuration=configuration))
        policy_index = PolicyIndex(
            freshness=Freshness(stale_after_seconds=settings.policy_resync_seconds * STALE_AFTER_CYCLES)
        )
        await stack.enter_async_context(
            background(
                PolicyInformer(
                    index=policy_index,
                    custom_objects=cast(CustomObjectsClient, CustomObjectsApi(api)),
                    core_v1=CoreV1Api(api),
                    policy_namespace=settings.policy_namespace,
                    caller_service_account_namespaces=settings.caller_service_account_namespaces,
                    resync_seconds=settings.policy_resync_seconds,
                ).run(),
                name="action-policy-informer",
            )
        )
        connections = ConnectionAuthority(make_sessionmaker(engine), policy_index)
        enrollments = EnrollmentAuthority(make_sessionmaker(engine), connections)
        mcp_linkage = await stack.enter_async_context(
            McpLinkageAuthority(
                make_sessionmaker(engine),
                settings.mcp_servers,
                client_metadata=settings.mcp_client_metadata,
                engine=engine,
            )
        )
        # Built unconditionally: a configured sandbox group must fail at startup rather than at the
        # first dispatch, and an API client this process already holds costs nothing when unused.
        sandboxes = SandboxClients(
            custom_objects=cast(CustomObjectsClient, CustomObjectsApi(api)),
            core_v1=CoreV1Api(api),
            exec_runner=KubernetesWebSocketExecRunner(configuration),
        )
        updates = ActionUpdateBroker(settings.database_url)
        executors = await stack.enter_async_context(
            running_executor(catalog, mcp_linkage, sandboxes, on_health_change=updates.wake_mcp_health)
        )
        if settings.web_push is not None:
            await stack.enter_async_context(
                ActionPushNotifier(
                    PushIdentity(settings.web_push),
                    PushSubscriptionStore(make_sessionmaker(engine)),
                    base_url=settings.web_push.public_base_url,
                    database_url=settings.database_url,
                )
            )

        def drain_backends() -> None:
            for executor in executors.values():
                executor.begin_drain()

        # Stop dispatch/lease tasks before closing the adapters, including failed service startup.
        service = await stack.enter_async_context(
            ActionService(
                ActionStore(make_sessionmaker(engine), external_grants=connections),
                catalog,
                executors,
                providers=[PolicySetDecisionProvider(visibility=visibility)],
                policies=policy_index,
                on_drain=drain_backends,
            )
        )
        operator_authenticator: OperatorAuthenticator
        if settings.operator_oidc is not None:
            operator_authenticator = OidcOperatorAuthenticator(settings.operator_oidc)
        else:
            operator_authenticator = DisabledOperatorAuthenticator()
            logger.info("operator/BFF API is disabled because no operator authenticator is configured")
        oauth = (
            await stack.enter_async_context(
                running_oauth(settings.oauth, settings.database_url, enrollments, connections)
            )
            if settings.oauth is not None
            else None
        )
        app = create_app(
            service,
            WorkloadPrincipalResolver(
                authentication=AuthenticationV1Api(api),
                audience=settings.token_audience,
                allowed_service_account_namespaces=settings.caller_service_account_namespaces,
            ),
            operator_authenticator,
            catalog,
            callers=policy_index,
            reader_accounts=settings.reader_accounts,
            connections=connections,
            updates=updates,
            enrollments=enrollments if oauth is not None else None,
            oauth=oauth,
            push_identity=PushIdentity(settings.web_push) if settings.web_push is not None else None,
            push_subscriptions=PushSubscriptionStore(make_sessionmaker(engine))
            if settings.web_push is not None
            else None,
            mcp_linkage=mcp_linkage,
            direct_wait_seconds=settings.direct_wait_seconds,
            max_wait_seconds=settings.max_wait_seconds,
        )
        await ActionServer(
            uvicorn.Config(app, host=settings.host, port=settings.port, timeout_graceful_shutdown=5), service
        ).serve()


if __name__ == "__main__":
    main()
