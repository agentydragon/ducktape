"""What the sandbox group offers: its roster, and each Action's definition from its own models.

Apart from the executor so the manifest generator can write the policy that auto-approves the
roster without importing the executor, and with it the service it runs in.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import cast

from pydantic import BaseModel, JsonValue

from agentplane.action_service.catalog import ActionDefinition
from agentplane.action_service.sandbox.binding import SandboxExecutorBinding
from agentplane.action_service.sandbox.models import (
    READY_CONDITION,
    CreateArgs,
    ExecArgs,
    NameArgs,
    NoArgs,
    TemplateArgs,
)

# The catalog key this group is configured under, and the namespace of every Action in it. Named
# here so the policy that auto-approves them cannot drift from the group that offers them.
SANDBOX_GROUP = "sandbox"


class SandboxAction(StrEnum):
    """This group's roster. A StrEnum and not bare strings because the executor's `match` compares
    against these by value, which a module-level constant would silently capture into instead."""

    CREATE = "create"
    GET_TEMPLATE = "get_template"
    EXEC = "exec"
    LIST = "list"
    GET = "get"
    DISPOSE = "dispose"


def _schema(model: type[BaseModel]) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], model.model_json_schema())


def actions(binding: SandboxExecutorBinding, descriptions: dict[str, str]) -> dict[str, ActionDefinition]:
    """What this group offers, declared from its own models rather than discovered over a wire.

    The offered templates, with what each says of itself (`descriptions`), are rendered into
    `create`'s description so an agent can choose one without another call: naming a template that
    is not offered is the most likely way to get `create` wrong.
    """
    offered = json.dumps(dict(sorted(descriptions.items())))
    return {
        SandboxAction.CREATE: ActionDefinition(
            description=(
                "Create or reach a sandbox that runs as your own ServiceAccount. Returns as soon as "
                "the object exists, before the box can run anything, so poll "
                f'{SandboxAction.GET} until its {READY_CONDITION!r} condition has status "True". '
                f"Idempotent on the name, so polling with {SandboxAction.CREATE} would also work but "
                f"tells you nothing more. Templates: {offered}. {SandboxAction.GET_TEMPLATE} shows one whole. "
                "The box and everything in it is deleted at its `expires_at`, "
                f"{binding.initial_ttl_seconds}s after creation unless an {SandboxAction.EXEC} keeps it longer."
            ),
            input_schema=_schema(CreateArgs),
        ),
        SandboxAction.GET_TEMPLATE: ActionDefinition(
            description=(
                f"One template that {SandboxAction.CREATE} offers, whole, as the API server holds it: the "
                "Pod each box made from it gets, with its images, resources, working directory, "
                f"environment and volumes. {SandboxAction.CREATE} changes one thing in that Pod: the box "
                "runs as your ServiceAccount, whatever `serviceAccountName` the template names."
            ),
            input_schema=_schema(TemplateArgs),
        ),
        SandboxAction.EXEC: ActionDefinition(
            description=(
                "Run one bounded Bash script in a ready sandbox of yours. A nonzero exit is a normal "
                f"result, not a failure. Timeout is capped at {binding.max_timeout_seconds}s and "
                f"retained output at {binding.max_output_bytes} bytes per stream, whatever you ask for. "
                f"Each run first keeps the box at least {binding.exec_ttl_extension_seconds}s past its start; "
                "a box already past its `expires_at` is refused."
            ),
            input_schema=_schema(ExecArgs),
        ),
        SandboxAction.LIST: ActionDefinition(
            description="Every sandbox you have here. Another account's are not listed and not reachable.",
            input_schema=_schema(NoArgs),
        ),
        SandboxAction.GET: ActionDefinition(
            description=(
                "Inspect one sandbox of yours without changing it: the controller's own conditions, "
                f'verbatim. Poll it until {READY_CONDITION!r} has status "True"; until then that '
                "condition's reason and message say what it is waiting on."
            ),
            input_schema=_schema(NameArgs),
        ),
        SandboxAction.DISPOSE: ActionDefinition(
            description="Delete one sandbox of yours, and everything in it. Disposing an absent one is not an error.",
            input_schema=_schema(NameArgs),
        ),
    }
