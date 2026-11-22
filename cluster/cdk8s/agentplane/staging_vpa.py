"""Resource request sizing for static Agentplane staging services and the Haku relay.

Agentplane staging opts out of Goldilocks because it also contains dynamic Sandboxes.
Haku CI opts out because it runs dynamically created Jobs.
"""

from cdk8s import App, Chart

from cluster.cdk8s.agentplane.namespaces import STAGING_NAMESPACE
from cluster.cdk8s.haku_ci import egress_gateway, runner as haku_ci_runner
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.vpa.policy import requests_only_vpa

NAME = "agentplane-staging-vpa"
OUTPUT_DIR = f"{GENERATED_ROOT}/agentplane-staging/vpa"

# (Deployment, container, memory floor, memory ceiling).
_WORKLOADS = (
    ("actions", "actions", "128Mi", "160Mi"),
    ("app", "app", "128Mi", "160Mi"),
    ("central-sampler", "central-sampler", None, None),
    ("egress", "proxy", None, None),
    ("electric", "electric", "448Mi", None),
    ("environments", "environments", "64Mi", "112Mi"),
    ("frontend", "frontend", None, None),
    ("history-service", "history-service", "128Mi", "256Mi"),
    ("llm-ingress", "ingress", "128Mi", "160Mi"),
    ("mcp-edge", "mcp-edge", "128Mi", "160Mi"),
    ("notifications", "notifications", "128Mi", "160Mi"),
    ("policy-controller", "policy-controller", "64Mi", "112Mi"),
    ("sessions", "sessions", "256Mi", "320Mi"),
)
_UPDATE_MODES = {"frontend": "Recreate"}


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    for deployment, container, memory_floor, memory_ceiling in _WORKLOADS:
        requests_only_vpa(
            chart,
            deployment,
            namespace=STAGING_NAMESPACE,
            container_name=container,
            update_mode=_UPDATE_MODES.get(deployment, "Initial"),
            min_memory=memory_floor,
            max_memory=memory_ceiling,
        )
    # The staging Deployment artifact already owns and health-checks the Haku relay.
    egress_gateway.add_vpa(chart, haku_ci_runner.NAMESPACE)
    return chart
