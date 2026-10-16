"""Build the Runner Proxy resources over a fake Kubernetes cluster."""

from agentplane.runner_gateway.connection_records import ConnectionLeases
from agentplane.runner_gateway.destinations import DestinationResolver
from agentplane.runner_gateway.lifecycle_control import LifecycleControl
from agentplane.runner_gateway.runner_proxy import Resources
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.principal import WorkloadPrincipalResolver


def build_resources(
    *,
    principals: WorkloadPrincipalResolver,
    destinations: DestinationResolver,
    caller_accounts: frozenset[ServiceAccountRef],
    sandbox_controller_account: ServiceAccountRef | None = None,
    control: LifecycleControl | None = None,
) -> Resources:
    return Resources(
        principals=principals,
        caller_accounts=caller_accounts,
        sandboxes=destinations.sandboxes,
        destinations=destinations,
        control=control or LifecycleControl(ConnectionLeases()),
        sandbox_controller_account=sandbox_controller_account,
    )
