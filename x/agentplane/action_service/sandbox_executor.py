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

import logging
from enum import StrEnum
from typing import Any, cast

from pydantic import BaseModel, JsonValue, ValidationError

from mcp_infra.exec.kubernetes import PodExecError
from x.agentplane.action_service.catalog import ActionDefinition
from x.agentplane.action_service.models import (
    ExecutionLease,
    ExecutionRequest,
    ExecutionResult,
    ExecutionState,
    Executor,
)
from x.agentplane.sandbox_actions.binding import SandboxExecutorBinding
from x.agentplane.sandbox_actions.inventory import ForeignSandboxError, SandboxActionError, SandboxInventory
from x.agentplane.sandbox_actions.models import (
    DisposeResult,
    ExecArgs,
    ExecResult,
    NameArgs,
    NoArgs,
    ProvisionArgs,
    SandboxList,
    SandboxState,
)
from x.agentplane.subjects import ServiceAccountRef

logger = logging.getLogger(__name__)


# The catalog key this group is configured under, and the namespace of every Action in it. Named
# here so the policy that auto-approves them cannot drift from the group that offers them.
SANDBOX_GROUP = "sandbox"


class SandboxAction(StrEnum):
    """This group's roster. A StrEnum and not bare strings because `match` below compares against
    these by value, which a module-level constant would silently capture into instead."""

    PROVISION = "provision"
    EXEC = "exec"
    LIST = "list"
    INFO = "info"
    DISPOSE = "dispose"


def _schema(model: type[BaseModel]) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], model.model_json_schema())


def actions(binding: SandboxExecutorBinding) -> dict[str, ActionDefinition]:
    """What this group offers, declared from its own models rather than discovered over a wire.

    The environment list is rendered into the description because it is deployment configuration an
    agent cannot otherwise see, and naming an environment that does not exist is the most likely way
    to get `provision` wrong.
    """
    # Descriptions are free text a deployment writes, so a trailing period is as likely as not and
    # would double up against the sentence this is embedded in.
    offered = "; ".join(
        f"{name}: {environment.description.rstrip('.')}" for name, environment in sorted(binding.environments.items())
    )
    return {
        SandboxAction.PROVISION: ActionDefinition(
            description=(
                "Create or reach a sandbox that runs as your own ServiceAccount, and wait for it to "
                f"come up. Idempotent on the name. Environments — {offered}. Defaults to "
                f"{binding.default_environment!r}. May return state={SandboxState.NOT_READY} with the "
                f"controller's reason if the box is slow; poll {SandboxAction.INFO} rather than "
                "provisioning again."
            ),
            input_schema=_schema(ProvisionArgs),
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
            description="Inspect one sandbox of yours without changing it; use it to poll a box that is not ready yet.",
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
        an outcome this cannot characterise is allowed to propagate."""
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
            return await self._dispatch(request.action.name, caller, request.arguments)
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
            case SandboxAction.PROVISION:
                args = ProvisionArgs.model_validate(arguments)
                info = await self._inventory.provision(caller, args.name, args.environment)
                return _succeeded(info)
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
