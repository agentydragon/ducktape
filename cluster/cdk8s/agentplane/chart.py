"""An environment's whole surface as one chart -- shared by generate_manifests (writes it
to disk) and tests (synthesize it in memory via `cdk8s.Testing`).
"""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import ConfigMap

from cluster.cdk8s.agentplane import (
    actions_constructs,
    app_constructs,
    db_constructs,
    egress_constructs,
    llm_ingress_constructs,
    namespace_rbac_constructs,
)
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.metadata import metadata
from util.settings_contract import settings_file
from x.agentplane.app import main as app_main


def environment_chart(app: App, env: Environment) -> Chart:
    chart = Chart(app, "agentplane", disable_resource_name_hashes=True)
    namespace_rbac_constructs.NamespaceQuota(chart, "namespace", env)
    namespace_rbac_constructs.AgentRbac(chart, "rbac", env)
    ConfigMap(
        chart,
        "config",
        metadata=metadata("agentplane-app-config", env.namespace),
        data={"config.yaml": yaml_config(settings_file(app_main.Settings, env.app_config))},
    )
    db_constructs.Db(chart, "db", env)
    llm_ingress_constructs.LlmIngress(chart, "llm-ingress", env)
    egress_constructs.Egress(chart, "egress", env)
    app_constructs.App(chart, "app", env)
    actions_constructs.Actions(chart, "actions", env)
    env.extra(chart)
    add_fleet_rules(
        chart,
        provided_secrets=env.provided_secrets,
        providers=frozenset({*env.depends_on, *env.extra_resources}),
        # The interception proxy terminates TLS for the namespace; its allowlist is the
        # EgressPolicy objects, not SNI on its own egress rule.
        unpinned_https_egress=frozenset({egress_constructs.NAME}),
    )
    return chart
