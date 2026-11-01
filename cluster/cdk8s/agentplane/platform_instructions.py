"""Render the deployment-wide Session guide and related environment values."""

from dataclasses import dataclass

from agentplane.egress.resources import placeholder_of
from agentplane.session_instructions.platform_instructions import SessionRuntime, render_platform_instructions
from cluster.cdk8s.agentplane import actions, egress, notifications, session_dispatcher
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.service_ref import ServiceRef

_WORKLOAD_PLACEHOLDER = "agentplane-credential-agentplane-workload"
_NOTIFICATIONS_PLACEHOLDER = "agentplane-credential-agentplane-notifications-workload"
_SESSIONS_PLACEHOLDER = placeholder_of(session_dispatcher.WORKLOAD_CREDENTIAL)


@dataclass(frozen=True)
class ServiceUrls:
    """The base URLs of the services a Session calls, as the platform instructions list them."""

    egress: str
    actions: str
    sessions: str
    notifications: str


def service_urls(env: Environment, *, egress_api_service: ServiceRef | None = None) -> ServiceUrls:
    actions_service = actions.service(env.namespace)
    sessions_service = session_dispatcher.service(env.namespace)
    notifications_http = notifications.http_service(env.namespace)
    egress_api = egress_api_service or egress.agent_api(env.namespace)
    egress_port = f":{egress_api.port.number}" if egress_api.port.number != 80 else ""
    return ServiceUrls(
        egress=f"http://{egress_api.fqdn}{egress_port}",
        actions=f"http://{actions_service.fqdn}:{actions_service.port.number}",
        sessions=f"http://{sessions_service.fqdn}:{sessions_service.port.number}",
        notifications=f"http://{notifications_http.fqdn}:{notifications_http.port.number}",
    )


def platform_instructions(
    env: Environment, *, session_runtime: SessionRuntime, egress_api_service: ServiceRef | None = None
) -> str:
    """Render the full platform guide with this environment's service URLs."""
    urls = service_urls(env, egress_api_service=egress_api_service)
    return render_platform_instructions(
        egress_api_url=urls.egress,
        actions_service_url=urls.actions,
        sessions_service_url=urls.sessions,
        notifications_service_url=urls.notifications,
        session_runtime=session_runtime,
    )


def agent_cli_environment(urls: ServiceUrls) -> dict[str, str]:
    """Return the service URLs and placeholder bearers read by the `agentplane` command."""
    return {
        "AGENTPLANE_ACTIONS_URL": urls.actions,
        "AGENTPLANE_ACTIONS_CREDENTIAL": _WORKLOAD_PLACEHOLDER,
        "AGENTPLANE_SESSIONS_URL": urls.sessions,
        "AGENTPLANE_SESSIONS_CREDENTIAL": _SESSIONS_PLACEHOLDER,
        "AGENTPLANE_NOTIFICATIONS_URL": urls.notifications,
        "AGENTPLANE_NOTIFICATIONS_CREDENTIAL": _NOTIFICATIONS_PLACEHOLDER,
        "AGENTPLANE_EGRESS_URL": urls.egress,
        "AGENTPLANE_EGRESS_CREDENTIAL": _WORKLOAD_PLACEHOLDER,
    }
