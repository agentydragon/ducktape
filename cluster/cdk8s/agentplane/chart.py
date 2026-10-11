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
    history_compaction,
    history_ingress,
    history_service,
    llm_ingress,
    notifications,
    runner_gateway,
    runner_sandbox,
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
    electric.Electric(chart, "electric", env, electric.APP)
    electric.Electric(chart, "electric-history", env, electric.HISTORY)
    llm_ingress.LlmIngress(chart, "llm-ingress", env)
    egress.Egress(chart, "egress", env, runners=runner_sandbox.pods(env.namespace))
    sandbox_pod.add_tool_config(chart, env)
    app_caller = {
        ServiceAccountRef(namespace=env.namespace, name=app_component.NAME): app_component.service(env.namespace).pods
    }
    notifications_caller = {
        ServiceAccountRef(namespace=env.namespace, name=notifications.NAME): notifications.service(env.namespace).pods
    }
    # The ingester places retention holds and follows every Session (WatchSessions, FollowSession).
    history_caller = {
        ServiceAccountRef(namespace=env.namespace, name=history_service.NAME): history_service.service(
            env.namespace
        ).pods
    }
    sandbox_service.SandboxService(
        chart, "sandbox-service", env, callers=app_caller | notifications_caller | history_caller
    )
    runner_gateway.RunnerGateway(
        chart,
        "runner-gateway",
        env,
        token_audience=sandbox_service.TOKEN_AUDIENCE,
        callers=app_caller | notifications_caller | history_caller,
        sealers={
            ServiceAccountRef(namespace=env.namespace, name=sandbox_service.NAME): sandbox_service.service(
                env.namespace
            ).pods
        },
        runners=runner_sandbox.pods(env.namespace),
    )
    history_service.HistoryService(
        chart,
        "history-service",
        env,
        # The app reads Threads; the Notification Service reads command receipts.
        callers=app_caller | notifications_caller,
        sandboxes=sandbox_service.service(env.namespace),
        sandboxes_audience=sandbox_service.TOKEN_AUDIENCE,
        gateway=runner_gateway.service(env.namespace),
    )
    history_ingress.HistoryIngress(chart, "history-ingress", env)
    if env.history_compaction is not None:
        history_compaction.HistoryCompaction(chart, "history-compaction", env, env.history_compaction)
    app_component.App(chart, "app", env)
    runner_sandbox.RunnerSandbox(chart, "runner-sandbox", env)
    actions.Actions(chart, "actions", env)
    notifications.Notifications(
        chart,
        "notifications",
        env,
        actions=actions.service(env.namespace),
        sandboxes=sandbox_service.service(env.namespace),
        gateway=runner_gateway.service(env.namespace),
        gateway_control_target=runner_gateway.control_target(env.namespace),
        history=history_service.service(env.namespace),
        history_audience=history_service.TOKEN_AUDIENCE,
        callers=[egress.proxy(env.namespace).pods, app_component.service(env.namespace).pods],
    )
    add_fleet_rules(
        chart,
        # The interception proxy terminates TLS for the namespace; its allowlist is the
        # EgressPolicy objects, not SNI on its own egress rule.
        unpinned_https_egress=frozenset({egress.NAME}),
    )
    return chart
