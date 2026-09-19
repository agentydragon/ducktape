"""An environment's whole surface as one chart -- shared by generate_manifests (writes it
to disk) and tests (synthesize it in memory via `cdk8s.Testing`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from cdk8s import App, Chart
from cdk8s_plus_34 import ConfigMap
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecHealthChecks,
)

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
from cluster.cdk8s.directory import Directory
from cluster.cdk8s.flux_constructs import health_checks
from cluster.cdk8s.metadata import metadata
from util.settings_contract import settings_file
from x.agentplane.app import main as app_main

# The chart objects whose readiness gates the environment, in the order the checks are
# listed. The trust-manager Bundle writes its target ConfigMap asynchronously, outside
# the rendered input, so that ConfigMap is checked explicitly rather than via `wait`.
_HEALTH_CHECK_KINDS = ("Namespace", "Cluster", "Database", "Deployment", "Certificate", "Bundle")
_CNPG_DATABASE_READY = (
    "has(status.applied) && status.applied && "
    "has(status.observedGeneration) && status.observedGeneration == metadata.generation"
)


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


def _bundle_config_map_checks(namespace: str) -> Callable[[Chart], Sequence[KustomizationSpecHealthChecks]]:
    """trust-manager names a Bundle's target ConfigMap after the Bundle."""

    def checks(chart: Chart) -> Sequence[KustomizationSpecHealthChecks]:
        return [
            KustomizationSpecHealthChecks(api_version="v1", kind="ConfigMap", name=check.name, namespace=namespace)
            for check in health_checks(chart, _HEALTH_CHECK_KINDS)
            if check.kind == "Bundle"
        ]

    return checks


def environment_directory(env: Environment) -> Directory:
    """This environment's `Directory` entry for the roster `generate_manifests.py` loops
    over -- its own `agentplane.k8s.yaml`, Flux Kustomization, and fleet rules."""
    return Directory(
        name=env.namespace,
        path=f"cluster/k8s/{env.namespace}",
        build=lambda app: environment_chart(app, env),
        depends_on=tuple(env.depends_on),
        provided_secrets=env.provided_secrets,
        # The interception proxy terminates TLS for the namespace; its allowlist is the
        # EgressPolicy objects, not SNI on its own egress rule.
        unpinned_https_egress=(egress_constructs.NAME,),
        extra_resources=tuple(env.extra_resources),
        image_pins=True,
        description=env.flux_description,
        health_check_kinds=_HEALTH_CHECK_KINDS,
        extra_health_checks=_bundle_config_map_checks(env.namespace),
        health_check_exprs=(
            KustomizationSpecHealthCheckExprs(
                api_version="postgresql.cnpg.io/v1", kind="Database", current=_CNPG_DATABASE_READY
            ),
        ),
        # This one Kustomization owns the CNPG Cluster's PVCs; pruning on deletion would
        # take the database with them.
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        wait=None,
    )
