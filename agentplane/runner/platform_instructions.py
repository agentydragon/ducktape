"""Render the deployment-wide platform instructions every Session starts with."""

from jinja2 import StrictUndefined, Template

from util.bazel.runfiles import get_required_path, own_repo_rlocation

PLATFORM_INSTRUCTIONS_TEMPLATE = "agentplane/runner/platform_instructions.j2"


def render_platform_instructions(
    *, egress_api_url: str, actions_service_url: str, notifications_service_url: str
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
            notifications_service_url=notifications_service_url,
        )
    ).strip()
