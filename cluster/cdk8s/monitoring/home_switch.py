"""Cluster side of the home MikroTik switch (CRS310): the read-only `monitoring` user's password.

ESO mints it here, and nothing else holds it. `tf/home-switch` reads this Secret and sets the
password on the switch's RouterOS `monitoring` user, which a RouterOS exporter will log in as.
"""

from __future__ import annotations

from cdk8s import App, Chart
from external_secrets_crds.io.external_secrets import ExternalSecretSpecTargetCreationPolicy

from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.secret_ref import SecretRef

NAMESPACE = "monitoring"
OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/home-switch"
# tf/home-switch/main.tf reads this Secret and key by name.
MONITORING_PASSWORD = SecretRef(namespace=NAMESPACE, name="home-switch-monitoring").key("password")


def chart(app: App) -> Chart:
    chart = Chart(app, "home-switch", disable_resource_name_hashes=True)
    mint_bearer_secret(
        chart,
        "monitoring-password",
        name=MONITORING_PASSWORD.secret.name,
        namespace=MONITORING_PASSWORD.secret.namespace,
        key=MONITORING_PASSWORD.key,
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        description="Password of the home switch's read-only RouterOS user; tf/home-switch sets it on the switch.",
    )
    add_fleet_rules(chart)
    return chart


def home_switch(
    flux_chart: Chart, directory: RenderedDirectory, external_secrets_operator: Kustomization
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        "monitoring-home-switch",
        directory,
        timeout="5m",
        depends_on=[flux_kustomization_depends_on(external_secrets_operator)],
    )
