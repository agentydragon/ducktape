"""The sandbox Actions as a code-owned executor.

A sandbox here is a shell as the caller: it runs as the ServiceAccount that submitted the Action,
reaches what that account's `EgressBinding`s allow, and is admitted to this service as that same
account. No caller obtains authority it did not already hold, which is what makes "is it safe to
bind this Action for account X" answerable: exactly as safe as X's own authority.

The identity comes from `ExecutionRequest.caller`, which the authenticated admission path sets and
no argument can reach. A surface that took the account as an argument would instead mean "stamp a
Pod as any account you can name".
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from typing import Any, cast

from pydantic import BaseModel, JsonValue, ValidationError

from agentplane.action_service.catalog import ActionDefinition
from agentplane.action_service.models import ExecutionLease, ExecutionRequest, ExecutionResult, ExecutionState, Executor
from agentplane.action_service.sandbox.binding import SandboxExecutorBinding
from agentplane.action_service.sandbox.inventory import ForeignSandboxError, SandboxActionError, SandboxInventory
from agentplane.action_service.sandbox.models import (
    READY_CONDITION,
    CreateArgs,
    DisposeResult,
    ExecArgs,
    ExecResult,
    NameArgs,
    NoArgs,
    SandboxList,
    TemplateArgs,
)
from agentplane.action_service.service import hold_lease
from agentplane.subjects import ServiceAccountRef
from mcp_infra.exec.kubernetes import PodExecError

logger = logging.getLogger(__name__)


# The catalog key this group is configured under, and the namespace of every Action in it. Named
# here so the policy that auto-approves them cannot drift from the group that offers them.
SANDBOX_GROUP = "sandbox"


class SandboxAction(StrEnum):
    """This group's roster. A StrEnum and not bare strings because `match` below compares against
    these by value, which a module-level constant would silently capture into instead."""

    CREATE = "create"
    GET_TEMPLATE = "get_template"
    EXEC = "exec"
    LIST = "list"
    INFO = "info"
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
                f'{SandboxAction.INFO} until its {READY_CONDITION!r} condition has status "True". '
                f"Idempotent on the name, so polling with {SandboxAction.CREATE} would also work but "
                f"tells you nothing more. Templates: {offered}. {SandboxAction.GET_TEMPLATE} shows one whole."
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
                f"retained output at {binding.max_output_bytes} bytes per stream, whatever you ask for."
            ),
            input_schema=_schema(ExecArgs),
        ),
        SandboxAction.LIST: ActionDefinition(
            description="Every sandbox you have here. Another account's are not listed and not reachable.",
            input_schema=_schema(NoArgs),
        ),
        SandboxAction.INFO: ActionDefinition(
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


def _failed(kind: str, message: str) -> ExecutionResult:
    return ExecutionResult(state=ExecutionState.FAILED, error={"kind": kind, "message": message})


class SandboxExecutor(Executor):
    """Runs the sandbox Actions in this process, against the caller the request names.

    Inherits the no-op `begin_drain`: a sandbox outlives any one call and is the caller's to
    dispose of, so shutdown must not touch one.
    """

    def __init__(self, binding: SandboxExecutorBinding, inventory: SandboxInventory) -> None:
        self._binding = binding
        self._inventory = inventory

    async def execute(self, request: ExecutionRequest, lease: ExecutionLease) -> ExecutionResult:
        """One dispatch. A refusal the caller can act on is a failed Execution with a reason; only
        an outcome this cannot characterise is allowed to propagate.

        The bound `hold_lease` requires is the one the command already carries: `timeout_seconds`
        stops the script in the Pod and the exec itself a little after that, so renewing here can
        outlast a lease window -- a clone of a large repository does -- but never run unbounded.
        """
        caller = request.caller
        if caller.namespace != self._binding.namespace:
            # A Pod can only run as a ServiceAccount in its own namespace, so a caller from anywhere
            # else cannot be given a sandbox that is it. Refusing says so; stamping the box as some
            # other account would quietly hand it authority its caller never had.
            return _failed(
                "caller_not_local",
                f"sandboxes run as their caller, so the calling ServiceAccount must live in "
                f"{self._binding.namespace!r}; {caller.namespace!r} cannot be given one",
            )
        try:
            return await hold_lease(lease, self._dispatch(request.action.name, caller, request.arguments))
        except ForeignSandboxError as error:
            return _failed("sandbox_not_yours", str(error))
        except SandboxActionError as error:
            return _failed("sandbox_unavailable", str(error))
        except PodExecError as error:
            return _failed("exec_failed", str(error))
        except ValidationError:
            # Pydantic's text echoes the submitted values back.
            return _failed("invalid_arguments", "arguments do not match this Action's schema")

    async def _dispatch(
        self, action: str, caller: ServiceAccountRef, arguments: dict[str, JsonValue]
    ) -> ExecutionResult:
        match action:
            case SandboxAction.CREATE:
                args = CreateArgs.model_validate(arguments)
                info = await self._inventory.create(caller, args.name, args.template)
                return _succeeded(info)
            case SandboxAction.GET_TEMPLATE:
                template_args = TemplateArgs.model_validate(arguments)
                template = await self._inventory.get_template(template_args.template)
                return ExecutionResult(state=ExecutionState.SUCCEEDED, result=cast(JsonValue, template))
            case SandboxAction.EXEC:
                exec_args = ExecArgs.model_validate(arguments)
                result = await self._inventory.execute(
                    caller,
                    exec_args.name,
                    script=exec_args.script,
                    cwd=exec_args.cwd,
                    timeout_seconds=exec_args.timeout_seconds,
                    max_output_bytes=exec_args.max_output_bytes,
                )
                return _succeeded(
                    ExecResult(
                        exit=result.exit,
                        stdout=result.stdout,
                        stderr=result.stderr,
                        duration_seconds=result.duration_seconds,
                    )
                )
            case SandboxAction.LIST:
                NoArgs.model_validate(arguments)
                return _succeeded(SandboxList(sandboxes=await self._inventory.list(caller)))
            case SandboxAction.INFO:
                name_args = NameArgs.model_validate(arguments)
                return _succeeded(await self._inventory.info(caller, name_args.name))
            case SandboxAction.DISPOSE:
                dispose_args = NameArgs.model_validate(arguments)
                existed = await self._inventory.dispose(caller, dispose_args.name)
                return _succeeded(DisposeResult(name=dispose_args.name, existed=existed))
            case _:
                # The catalog admitted it, so this is a roster that grew without a branch.
                return _failed("unknown_action", f"this executor has no {action!r}")


def _succeeded(payload: BaseModel) -> ExecutionResult:
    return ExecutionResult(state=ExecutionState.SUCCEEDED, result=cast(Any, payload.model_dump(mode="json")))
