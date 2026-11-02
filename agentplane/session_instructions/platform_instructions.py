"""Render the deployment-wide platform instructions every Session starts with."""

from enum import StrEnum

from jinja2 import StrictUndefined, Template

from util.bazel.runfiles import get_required_path, own_repo_rlocation

PLATFORM_INSTRUCTIONS_TEMPLATE = "agentplane/session_instructions/platform_instructions.j2"


class SessionRuntime(StrEnum):
    NATIVE_HARNESS = "native_harness"
    CENTRAL_SAMPLER = "central_sampler"


def render_platform_instructions(
    *,
    egress_api_url: str,
    actions_service_url: str,
    sessions_service_url: str,
    notifications_service_url: str,
    session_runtime: SessionRuntime,
) -> str:
    """Render the complete deployment-wide prompt from service URLs and shared guidance."""
    template = Template(
        get_required_path(own_repo_rlocation(PLATFORM_INSTRUCTIONS_TEMPLATE)).read_text(encoding="utf-8"),
        undefined=StrictUndefined,
    )
    return str(
        template.render(
            egress_api_url=egress_api_url,
            actions_service_url=actions_service_url,
            sessions_service_url=sessions_service_url,
            notifications_service_url=notifications_service_url,
            session_runtime=session_runtime,
        )
    ).strip()
