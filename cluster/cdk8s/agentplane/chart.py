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
from cluster.cdk8s.metadata import metadata
from x.agentplane.app import main as app_main
from x.agentplane.settings_contract import settings_file


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
    return chart
