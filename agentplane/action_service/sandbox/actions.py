"""What the sandbox group offers: its roster, and each Action's definition from its own models.

Apart from the executor so the manifest generator can write the policy that auto-approves the
roster without importing the executor, and with it the service it runs in.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import cast

from mcp.types import ToolAnnotations
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


def _exec_schema(binding: SandboxExecutorBinding) -> dict[str, JsonValue]:
    """`ExecArgs` with this deployment's caps as its maxima, so submission refuses a request the
    executor would cut short, and says why, instead of the run ending early."""
    # TODO: find a way to carry the caps into the schema other than overwriting the maxima
    # `ExecArgs` generates by hand.
    schema = ExecArgs.model_json_schema()
    schema["properties"]["timeout_seconds"]["maximum"] = binding.max_timeout_seconds
    schema["properties"]["max_output_bytes"]["maximum"] = binding.max_output_bytes
    return cast(dict[str, JsonValue], schema)


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
                f"tells you nothing more. Templates: {offered}. {SandboxAction.GET_TEMPLATE} shows one whole."
            ),
            input_schema=_schema(CreateArgs),
            title="Create sandbox",
            annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True),
        ),
        SandboxAction.GET_TEMPLATE: ActionDefinition(
            description=(
                f"One template that {SandboxAction.CREATE} offers, whole, as the API server holds it: the "
                "Pod each box made from it gets, with its images, resources, working directory, "
                f"environment and volumes. {SandboxAction.CREATE} changes one thing in that Pod: the box "
                "runs as your ServiceAccount, whatever `serviceAccountName` the template names."
            ),
            input_schema=_schema(TemplateArgs),
            title="Show sandbox template",
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        ),
        SandboxAction.EXEC: ActionDefinition(
            description=(
                "Run one bounded Bash script in a ready sandbox of yours. A nonzero exit is a normal "
                "result, not a failure."
            ),
            input_schema=_exec_schema(binding),
            title="Run script in sandbox",
            annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False),
        ),
        SandboxAction.LIST: ActionDefinition(
            description="Every sandbox you have here. Another account's are not listed and not reachable.",
            input_schema=_schema(NoArgs),
            title="List sandboxes",
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        ),
        SandboxAction.GET: ActionDefinition(
            description=(
                "Inspect one sandbox of yours without changing it: the controller's own conditions, "
                f'verbatim. Poll it until {READY_CONDITION!r} has status "True"; until then that '
                "condition's reason and message say what it is waiting on."
            ),
            input_schema=_schema(NameArgs),
            title="Inspect sandbox",
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        ),
        SandboxAction.DISPOSE: ActionDefinition(
            description="Delete one sandbox of yours, and everything in it. Disposing an absent one is not an error.",
            input_schema=_schema(NameArgs),
            title="Dispose sandbox",
            annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True),
        ),
    }
