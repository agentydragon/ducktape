"""The objects every environment has, as one chart. `staging.chart` and `testing.chart`
each call this and add their environment-only objects to what it returns; those are what
generate_manifests (writes them to disk) and the tests (synthesize them in memory via
`cdk8s.Testing`) build.
"""

from __future__ import annotations

from cdk8s import App, Chart

from agentplane.sandbox_service.kubevirt import VmTemplate
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s.agentplane import (
    actions,
    app as app_component,
    database,
    egress,
    electric,
    llm_ingress,
    rbac,
    sandbox_pod,
    sandbox_service,
)
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.fleet_rules import add_fleet_rules


def environment_chart(
    app: App, env: Environment, *, vm_templates: dict[str, VmTemplate] | None = None, vm_relay_image: str | None = None
) -> Chart:
    """The shared objects. The fleet rules attach as a synth-time validation, so a caller
    may keep adding objects to the returned chart and they are still checked.

    Deliberately excludes `rbac.AgentRbac` -- that Role/RoleBinding lets an agent drive
    Agentplane without a human, which only belongs in `testing.chart` (see
    `rbac.AgentRbac`'s own docstring)."""
    chart = Chart(app, "agentplane", disable_resource_name_hashes=True)
    rbac.NamespaceQuota(chart, "namespace", env)
    database.Db(chart, "db", env)
    electric.Electric(chart, "electric", env)
    llm_ingress.LlmIngress(chart, "llm-ingress", env)
    egress.Egress(chart, "egress", env)
    sandbox_pod.add_tool_config(chart, env)
    sandbox_service.SandboxService(
        chart,
        "sandbox-service",
        env,
        manager=ServiceAccountRef(namespace=env.namespace, name=app_component.NAME),
        caller=app_component.service(env.namespace),
        vm_templates=vm_templates,
        vm_relay_image=vm_relay_image,
    )
    app_component.App(chart, "app", env)
    actions.Actions(chart, "actions", env)
    add_fleet_rules(
        chart,
        # The interception proxy terminates TLS for the namespace; its allowlist is the
        # EgressPolicy objects, not SNI on its own egress rule.
        unpinned_https_egress=frozenset({egress.NAME}),
    )
    return chart
