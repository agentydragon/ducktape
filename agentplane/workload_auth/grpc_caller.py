"""Admit a gRPC caller by its workload token and a ServiceAccount allowlist."""

from collections.abc import Set

import grpc

from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.bearer import parse_bearer, sole_header
from agentplane.workload_auth.principal import WorkloadPrincipalRejectedError, WorkloadPrincipalResolver

# gazelle:include_dep @pypi//grpcio


async def authenticate(
    principals: WorkloadPrincipalResolver, accounts: Set[ServiceAccountRef], context: grpc.aio.ServicerContext
) -> ServiceAccountRef:
    """The caller's account; aborts with PERMISSION_DENIED for an account outside `accounts`."""
    values = [value for key, value in (context.invocation_metadata() or ()) if key == "authorization"]
    if any(not isinstance(value, str) for value in values):
        raise WorkloadPrincipalRejectedError("invalid bearer metadata")
    value = sole_header([value for value in values if isinstance(value, str)])
    token = parse_bearer(value) if value is not None else None
    if token is None:
        raise WorkloadPrincipalRejectedError("invalid bearer metadata")
    principal = await principals.resolve_workload(token)
    if principal.account not in accounts:
        await context.abort(grpc.StatusCode.PERMISSION_DENIED, "service caller not allowed")
    return principal.account
