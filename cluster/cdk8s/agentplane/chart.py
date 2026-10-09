"""The objects every environment has, as one chart. `staging.chart` and `testing.chart`
each call this and add their environment-only objects to what it returns; those are what
generate_manifests (writes them to disk) and the tests (synthesize them in memory via
`cdk8s.Testing`) build.
"""

from __future__ import annotations

from cdk8s import App, Chart
from constructs import Construct

from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import namespaces
from cluster.cdk8s.agentplane import (
    actions,
    app as app_component,
    database,
    egress,
    electric,
    llm_ingress,
    notifications,
    sandbox_pod,
    sandbox_service,
)
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.namespaces import Vpa


class Namespace(Construct):
    """The namespace shared by an Agentplane environment."""

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        namespaces.namespace(
            self,
            "namespace",
            name=env.namespace,
            # Runner Pods are Sandbox-owned, not Deployments; nothing here is VPA-managed.
            vpa=Vpa.DISABLED,
            # Standing agent access to metadata and logs (Kyverno-generated bindings).
            # Environment-specific write access is layered on by its chart.
            labels={"name": env.namespace},
            annotations={"description": env.description},
        )


def environment_chart(app: App, env: Environment) -> Chart:
    """The shared objects. The fleet rules attach as a synth-time validation, so a caller
    may keep adding objects to the returned chart and they are still checked.

    Deliberately excludes testing-only RBAC and namespace resource limits; `testing.chart`
    adds them."""
    chart = Chart(app, "agentplane", disable_resource_name_hashes=True)
    Namespace(chart, "namespace", env)
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
    )
    app_component.App(chart, "app", env)
    actions.Actions(chart, "actions", env)
    notifications.Notifications(
        chart,
        "notifications",
        env,
        actions=actions.service(env.namespace),
        sandboxes=sandbox_service.service(env.namespace),
    )
    add_fleet_rules(
        chart,
        # The interception proxy terminates TLS for the namespace; its allowlist is the
        # EgressPolicy objects, not SNI on its own egress rule.
        unpinned_https_egress=frozenset({egress.NAME}),
    )
    return chart
