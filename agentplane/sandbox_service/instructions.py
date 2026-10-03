"""Backend-owned operational instructions, also used by the app during the cutover."""

from jinja2 import StrictUndefined, Template

from agentplane.notification_service.instructions import instructions as notification_instructions
from util.bazel.runfiles import get_required_path

DEFAULT_AGENT_INSTRUCTIONS_TEMPLATE = "_main/agentplane/sandbox_service/agent_instructions.j2"


def resolved_agent_instructions(
    configured: str | None,
    *,
    egress_api_url: str | None,
    actions_service_url: str | None,
    notifications_service_url: str | None = None,
) -> str:
    """Use the image-owned instructions unless deployment configuration explicitly replaces them."""
    if configured is not None:
        return (
            combine_instructions(configured, notification_instructions(notifications_service_url))
            if notifications_service_url
            else configured
        )
    if egress_api_url is None or actions_service_url is None:
        raise ValueError("image-owned agent instructions require agent_egress_api_url and agent_actions_service_url")
    template = Template(
        get_required_path(DEFAULT_AGENT_INSTRUCTIONS_TEMPLATE).read_text(encoding="utf-8"), undefined=StrictUndefined
    )
    rendered = str(template.render(egress_api_url=egress_api_url, actions_service_url=actions_service_url))
    return (
        combine_instructions(rendered, notification_instructions(notifications_service_url))
        if notifications_service_url
        else rendered
    )


def combine_instructions(platform: str, task: str) -> str:
    """Prepend operational guidance without replacing the caller's task instructions."""
    return "\n\n".join(part.strip() for part in (platform, task) if part.strip())
