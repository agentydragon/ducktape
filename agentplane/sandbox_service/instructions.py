"""Backend-owned operational instructions."""

from jinja2 import StrictUndefined, Template

from agentplane.notification_service.instructions import instructions as notification_instructions
from util.bazel.runfiles import get_required_path

AGENT_INSTRUCTIONS_TEMPLATE = "_main/agentplane/sandbox_service/agent_instructions.j2"
_TIMEZONE_INSTRUCTIONS = (
    "Use the sandbox's configured local timezone (from the `TZ` environment variable) for human-facing "
    "dates and times; check `date` when needed. For APIs and machine-readable timestamps, use UTC or an explicit "
    "offset."
)


def render_agent_instructions_template(*, egress_api_url: str, actions_service_url: str) -> str:
    """Render the shared instruction template for an explicit deployment configuration."""
    template = Template(
        get_required_path(AGENT_INSTRUCTIONS_TEMPLATE).read_text(encoding="utf-8"), undefined=StrictUndefined
    )
    return str(template.render(egress_api_url=egress_api_url, actions_service_url=actions_service_url))


def resolved_agent_instructions(configured: str, *, notifications_service_url: str | None = None) -> str:
    """Augment explicitly configured platform instructions with invariant runtime guidance."""
    if not configured.strip():
        raise ValueError("agent_instructions must be configured")

    platform = combine_instructions(configured, _TIMEZONE_INSTRUCTIONS)
    if notifications_service_url:
        platform = combine_instructions(platform, notification_instructions(notifications_service_url))
    return platform


def combine_instructions(platform: str, task: str) -> str:
    """Prepend operational guidance without replacing the caller's task instructions."""
    return "\n\n".join(part.strip() for part in (platform, task) if part.strip())
