"""Render the deployment-wide Session guide and related environment values."""

from dataclasses import dataclass

from agentplane.session_instructions.platform_instructions import SessionRuntime, render_platform_instructions
from cluster.cdk8s.agentplane import actions, platform_routes, session_dispatcher
from cluster.cdk8s.agentplane.environment import Environment


@dataclass(frozen=True)
class ServiceUrls:
    """The base URLs of the services a Session calls, as the platform instructions list them."""

    egress_grpc: str
    actions: str
    sessions: str
    notifications_grpc: str


def service_urls(env: Environment, *, egress_host: str = "127.0.0.1") -> ServiceUrls:
    actions_service = actions.service(env.namespace)
    sessions_service = session_dispatcher.service(env.namespace)
    return ServiceUrls(
        egress_grpc=f"http://{egress_host}:{platform_routes.egress_rules(env.namespace).listen_port}",
        actions=f"http://{actions_service.fqdn}:{actions_service.port.number}",
        sessions=f"http://{sessions_service.fqdn}:{sessions_service.port.number}",
        notifications_grpc=f"http://{egress_host}:{platform_routes.notifications_grpc(env.namespace).listen_port}",
    )


def platform_instructions(env: Environment, *, session_runtime: SessionRuntime, egress_host: str = "127.0.0.1") -> str:
    """Render the full platform guide with this environment's service URLs."""
    urls = service_urls(env, egress_host=egress_host)
    return render_platform_instructions(
        egress_api_url=urls.egress_grpc,
        actions_service_url=urls.actions,
        sessions_service_url=urls.sessions,
        session_runtime=session_runtime,
    )


def agent_cli_environment(urls: ServiceUrls) -> dict[str, str]:
    """Return first-party service URLs; callers configure bearer credentials only when needed."""
    return {
        "AGENTPLANE_ACTIONS_URL": urls.actions,
        "AGENTPLANE_SESSIONS_URL": urls.sessions,
        "AGENTPLANE_NOTIFICATIONS_GRPC_URL": urls.notifications_grpc,
        "AGENTPLANE_EGRESS_GRPC_URL": urls.egress_grpc,
    }
