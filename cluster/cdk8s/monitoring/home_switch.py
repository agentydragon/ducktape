"""The home MikroTik switch (CRS310): the `tf/gitops/home-switch` Terraform and the two RouterOS
passwords it uses, both minted here by ESO and held nowhere else.

- `tofu`: the full-access user the Terraform logs in as. `tf/gitops/home-switch/bootstrap.sh`
  creates it on the switch, once per factory reset.
- `monitoring`: the read-only user the Terraform manages, which a RouterOS exporter will log in as.
"""

from __future__ import annotations

from cdk8s import App, Chart
from external_secrets_crds.io.external_secrets import ExternalSecretSpecTargetCreationPolicy

from cluster.cdk8s import terraform
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.secret_ref import SecretKey, SecretRef

NAME = "home-switch"
NAMESPACE = "monitoring"
OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/{NAME}"
# tf/gitops/home-switch/main.tf and bootstrap.sh read these Secrets and keys by name.
TOFU_PASSWORD = SecretRef(namespace=NAMESPACE, name="home-switch-tofu").key("password")
MONITORING_PASSWORD = SecretRef(namespace=NAMESPACE, name="home-switch-monitoring").key("password")
# The switch is reachable only from the home LAN; OptiPlex is the cluster node on it.
_HOME_LAN = {"topology.kubernetes.io/zone": "home-lan"}


def _mint(chart: Chart, id: str, key: SecretKey, description: str) -> None:
    mint_bearer_secret(
        chart,
        id,
        name=key.secret.name,
        namespace=key.secret.namespace,
        key=key.key,
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        description=description,
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    _mint(
        chart,
        "tofu-password",
        TOFU_PASSWORD,
        "Password of the home switch's full-access RouterOS user tf/gitops/home-switch logs in as.",
    )
    _mint(
        chart,
        "monitoring-password",
        MONITORING_PASSWORD,
        "Password of the home switch's read-only RouterOS user; tf/gitops/home-switch sets it on the switch.",
    )
    terraform.gitops_terraform(chart, "terraform", name=NAME, variables=None, node_selector=_HOME_LAN)
    add_fleet_rules(chart)
    return chart


def home_switch(
    flux_chart: Chart,
    directory: RenderedDirectory,
    external_secrets_operator: Kustomization,
    tofu_controller: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        "monitoring-home-switch",
        directory,
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(external_secrets_operator, tofu_controller),
    )
