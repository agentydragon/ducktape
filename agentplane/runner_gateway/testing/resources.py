"""The gateway's `Resources` over one test database, with one-second runner deadlines and no
runner control connection."""

from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.runner_gateway.connection_records import ConnectionLeases
from agentplane.runner_gateway.destinations import DestinationResolver
from agentplane.runner_gateway.grpc_api import Resources
from agentplane.runner_gateway.lifecycle_control import LifecycleControl
from agentplane.runner_gateway.session_registry import SessionRegistry
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.principal import WorkloadPrincipalResolver


def build_resources(
    engine: AsyncEngine,
    *,
    principals: WorkloadPrincipalResolver,
    destinations: DestinationResolver,
    caller_accounts: frozenset[ServiceAccountRef],
    platform_instructions: str,
    runner_admission_ack_timeout_s: float = 1,
) -> Resources:
    return Resources(
        admission_timeout_s=15,
        runner_admission_ack_timeout_s=runner_admission_ack_timeout_s,
        lifecycle_timeout_s=300,
        principals=principals,
        destinations=destinations,
        caller_accounts=caller_accounts,
        platform_instructions=platform_instructions,
        sessions=SessionRegistry(engine),
        control=LifecycleControl(ConnectionLeases()),
    )
