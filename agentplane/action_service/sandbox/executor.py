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
from typing import Any, cast

from pydantic import BaseModel, JsonValue, ValidationError

from agentplane.action_service.models import ExecutionLease, ExecutionRequest, ExecutionResult, ExecutionState, Executor
from agentplane.action_service.sandbox.actions import SandboxAction
from agentplane.action_service.sandbox.binding import SandboxExecutorBinding
from agentplane.action_service.sandbox.inventory import ForeignSandboxError, SandboxActionError, SandboxInventory
from agentplane.action_service.sandbox.models import (
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
            case SandboxAction.GET:
                name_args = NameArgs.model_validate(arguments)
                return _succeeded(await self._inventory.get(caller, name_args.name))
            case SandboxAction.DISPOSE:
                dispose_args = NameArgs.model_validate(arguments)
                existed = await self._inventory.dispose(caller, dispose_args.name)
                return _succeeded(DisposeResult(name=dispose_args.name, existed=existed))
            case _:
                # The catalog admitted it, so this is a roster that grew without a branch.
                return _failed("unknown_action", f"this executor has no {action!r}")


def _succeeded(payload: BaseModel) -> ExecutionResult:
    return ExecutionResult(state=ExecutionState.SUCCEEDED, result=cast(Any, payload.model_dump(mode="json")))
