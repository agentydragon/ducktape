"""The runner's `Runner` service, served by the replica holding a Sandbox's control stream.

A call is forwarded as a tunnel `Call` without decoding its messages, so the proxy serves every
method of the service and answers with the runner's own status and bytes. The callers' accounts may
call every method but `Quiesce`, which only the Sandbox controller may. Contract:
`agentplane/docs/runner_control.md` § Tunnelled calls.
"""

from dataclasses import dataclass
from typing import override
from uuid import UUID

import grpc

from agentplane.grpc_servicer import Abort, ServicerResources, status_mapping
from agentplane.proto import runner_control_pb2
from agentplane.runner_gateway.lifecycle_control import ConnectionNotHeldError, LifecycleControl
from agentplane.sandbox_service.errors import SandboxNotFoundError
from agentplane.sandbox_service.reader import SandboxReader
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.grpc_caller import authenticate

# gazelle:include_dep //agentplane/proto:runner_control_pb2
# gazelle:include_dep @pypi//grpcio
# gazelle:include_dep @pypi//protobuf

# gRPC servers do not expose the dialed `:authority`, so callers name the Sandbox in this header, the
# same name their target carries (`<sandbox>-control.<namespace>.svc`).
SANDBOX_HEADER = "x-agentplane-sandbox"

_RUNNER = runner_control_pb2.DESCRIPTOR.services_by_name["Runner"]
_CONTROLLER_ONLY = {f"/{_RUNNER.full_name}/Quiesce"}


@dataclass(frozen=True)
class Resources(ServicerResources):
    """What the proxy serves from; `caller_accounts` may call every method but `Quiesce`."""

    sandboxes: SandboxReader
    control: LifecycleControl
    sandbox_controller_account: ServiceAccountRef | None


errors = status_mapping(
    Abort(SandboxNotFoundError, (grpc.StatusCode.NOT_FOUND, "sandbox not found")),
    Abort(ValueError, (grpc.StatusCode.INVALID_ARGUMENT, f"exactly one {SANDBOX_HEADER} header is required")),
    # The runner's own refusal, or the connection's loss or deadline, as the runner connection reported it.
    Abort(grpc.aio.AioRpcError, lambda error: (error.code(), error.details() or "")),
    deadline="service deadline expired",
    unavailable="destination unavailable; no offline admission",
    also_unavailable=(ConnectionNotHeldError,),
)


def sandbox_name(context: grpc.aio.ServicerContext) -> str:
    names = [value for key, value in context.invocation_metadata() or () if key == SANDBOX_HEADER]
    if len(names) != 1 or not isinstance(names[0], str) or not names[0]:
        raise ValueError(f"exactly one {SANDBOX_HEADER} header is required")
    return names[0]


class RunnerProxy(grpc.GenericRpcHandler):
    def __init__(self, resources: Resources) -> None:
        self.resources = resources
        self._methods = {f"/{_RUNNER.full_name}/{method.name}" for method in _RUNNER.methods}

    @override
    def service(self, handler_call_details: grpc.HandlerCallDetails) -> grpc.RpcMethodHandler | None:
        method = handler_call_details.method
        if method not in self._methods:
            return None

        async def forward(request: bytes, context: grpc.aio.ServicerContext) -> bytes:
            return await self.forward(method, request, context)

        # No (de)serializers: the request and response pass through as bytes.
        return grpc.unary_unary_rpc_method_handler(forward)

    def callers(self, method: str) -> frozenset[ServiceAccountRef]:
        if method not in _CONTROLLER_ONLY:
            return self.resources.caller_accounts
        controller = self.resources.sandbox_controller_account
        return frozenset() if controller is None else frozenset({controller})

    async def forward(self, method: str, request: bytes, context: grpc.aio.ServicerContext) -> bytes:
        async with errors(context):
            await authenticate(self.resources.principals, self.callers(method), context)
            sandbox = await self.resources.sandboxes.get(sandbox_name(context))
            connection = self.resources.control.holder(UUID(sandbox.uid))
            if connection is None:
                raise ConnectionNotHeldError("another replica holds this Sandbox's runner connection")
            return await connection.tunnel.forward(method, request, deadline_s=context.time_remaining())
