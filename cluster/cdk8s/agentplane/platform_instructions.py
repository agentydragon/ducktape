"""Render the deployment-wide Session guide and related environment values."""

from dataclasses import dataclass

from agentplane.session_instructions.platform_instructions import SessionRuntime, render_platform_instructions
from cluster.cdk8s.agentplane import actions, platform_routes, runner_gateway
from cluster.cdk8s.agentplane.environment import Environment


@dataclass(frozen=True)
class ServiceUrls:
    """The base URLs of the services a Session calls, as the platform instructions list them."""

    platform_routes: str
    actions: str
    sessions: str


def service_urls(env: Environment, *, egress_host: str = "127.0.0.1") -> ServiceUrls:
    actions_service = actions.service(env.namespace)
    sessions_service = runner_gateway.sessions_service(env.namespace)
    return ServiceUrls(
        platform_routes=platform_routes.listener_url(host=egress_host),
        actions=f"http://{actions_service.authority}",
        sessions=f"http://{sessions_service.authority}",
    )


def platform_instructions(env: Environment, *, session_runtime: SessionRuntime, egress_host: str = "127.0.0.1") -> str:
    """Render the full platform guide with this environment's service URLs."""
    urls = service_urls(env, egress_host=egress_host)
    return render_platform_instructions(
        egress_api_url=urls.platform_routes,
        actions_service_url=urls.actions,
        sessions_service_url=urls.sessions,
        environments_service_url=urls.platform_routes,
        session_runtime=session_runtime,
    )


def agent_cli_environment(urls: ServiceUrls) -> dict[str, str]:
    """Return first-party service URLs; callers configure bearer credentials only when needed."""
    return {
        "AGENTPLANE_ACTIONS_URL": urls.actions,
        "AGENTPLANE_SESSIONS_URL": urls.sessions,
        "AGENTPLANE_ENVIRONMENTS_URL": urls.platform_routes,
        "AGENTPLANE_HISTORY_URL": urls.platform_routes,
        "AGENTPLANE_NOTIFICATIONS_GRPC_URL": urls.platform_routes,
        "AGENTPLANE_EGRESS_GRPC_URL": urls.platform_routes,
    }
