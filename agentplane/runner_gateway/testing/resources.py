"""The gateway's `Resources` over one test database, with one-second runner deadlines and no
runner control connection."""

from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.runner_gateway.command_delivery import CommandDelivery
from agentplane.runner_gateway.commands.connections import ConnectionStore
from agentplane.runner_gateway.commands.dispatch import DispatchStore
from agentplane.runner_gateway.commands.store import SubmissionStore
from agentplane.runner_gateway.connection_records import ConnectionLeases
from agentplane.runner_gateway.grpc_api import Resources
from agentplane.runner_gateway.lifecycle_control import LifecycleControl
from agentplane.runner_gateway.session_registry import SessionRegistry
from agentplane.sandbox_service.destinations import DestinationResolver
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
    # Shared, as in production: delivery and control must agree on which replica holds a runner.
    leases = ConnectionLeases()
    return Resources(
        runner_admission_ack_timeout_s=runner_admission_ack_timeout_s,
        principals=principals,
        destinations=destinations,
        caller_accounts=caller_accounts,
        platform_instructions=platform_instructions,
        sessions=SessionRegistry(engine),
        submissions=SubmissionStore(engine),
        delivery=CommandDelivery(
            engine.url,
            dispatches=DispatchStore(engine),
            connections=ConnectionStore(engine),
            leases=leases,
            liveness_s=1,
        ),
        control=LifecycleControl(leases),
    )
