"""The home MikroTik switch (CRS310): the `tf/gitops/home-switch` Terraform, and what it installs
on the switch.

Two RouterOS passwords, held nowhere but here and on the switch:

- `tofu`: the full-access user the Terraform logs in as. `tf/gitops/home-switch/bootstrap.sh`
  creates it on the switch with a fresh password, which it writes to this directory's
  `tofu-password.sops.yaml`; re-running it rotates the password.
- `monitoring`: the read-only user the Terraform manages, which a RouterOS exporter will log in as.
  ESO mints it.

cert-manager issues the switch's api-ssl/www-ssl certificate from the cluster CA, so clients
verify it against the CA bundle: the Terraform installs and renews `home-switch-tls`, and
`bootstrap.sh` installs `home-switch-bootstrap-tls`, which is good enough for the Terraform's
first connection.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from external_secrets_crds.io.external_secrets import ExternalSecretSpecTargetCreationPolicy
from pydantic import BaseModel, ConfigDict, Field
from tofu_controller.io.fluxcd.contrib.infra import (
    TerraformV1Alpha2SpecRunnerPodTemplateSpecVolumeMounts,
    TerraformV1Alpha2SpecRunnerPodTemplateSpecVolumes,
    TerraformV1Alpha2SpecRunnerPodTemplateSpecVolumesConfigMap,
    TerraformV1Alpha2SpecStoreReadablePlan,
)

from cluster.cdk8s import terraform
from cluster.cdk8s.cert_manager import cluster_ca
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.providers.cert_manager.certificate import Certificate, CertificatePrivateKey
from cluster.cdk8s.secret_ref import SecretKey, SecretRef

NAME = "home-switch"
NAMESPACE = "monitoring"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}"
# Written by tf/gitops/home-switch/bootstrap.sh, which names the Secret `home-switch-tofu-password`.
TOFU_PASSWORD_FILE = "tofu-password.sops.yaml"
# tf/gitops/home-switch/main.tf and bootstrap.sh read these Secrets and keys by name.
MONITORING_PASSWORD = SecretRef(namespace=NAMESPACE, name="home-switch-monitoring").key("password")
TLS_SECRET = "home-switch-tls"
BOOTSTRAP_TLS_SECRET = "home-switch-bootstrap-tls"
# The switch's DHCP lease, which its certificates name (tf/gitops/home-switch connects to it).
_SWITCH_ADDRESS = "192.168.1.100"
# bootstrap.sh repeats it.
_LAN_CIDR = "192.168.1.0/24"
# The switch is reachable only from the home LAN; OptiPlex is the cluster node on it.
_HOME_LAN = {"topology.kubernetes.io/zone": "home-lan"}
# main.tf's provider `ca_certificate` reads the bundle here.
_CA_BUNDLE_DIR = "/etc/cluster-ca"


class HomeSwitchVars(BaseModel):
    """The inputs of tf/gitops/home-switch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    switch_address: str = Field(description="The switch's management address, which its certificates name.")
    lan_cidr: str = Field(
        description="The only network management services and the monitoring user accept connections from."
    )


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
        "monitoring-password",
        MONITORING_PASSWORD,
        "Password of the home switch's read-only RouterOS user; tf/gitops/home-switch sets it on the switch.",
    )
    for id, secret_name in (("tls", TLS_SECRET), ("bootstrap-tls", BOOTSTRAP_TLS_SECRET)):
        Certificate(
            chart,
            id,
            metadata=ApiObjectMetadata(name=secret_name, namespace=NAMESPACE),
            secret_name=secret_name,
            issuer_ref=cluster_ca.INTERNAL_ISSUER,
            # main.tf's routeros_system_certificate names the same common name.
            common_name="CRS310",
            ip_addresses=[_SWITCH_ADDRESS],
            private_key=CertificatePrivateKey.ecdsa_p256(),
        )
    terraform.gitops_terraform(
        chart,
        "terraform",
        name=NAME,
        variables=HomeSwitchVars(switch_address=_SWITCH_ADDRESS, lan_cidr=_LAN_CIDR),
        # Plans every interval and reports drift; a person approves each apply (README.md).
        auto_apply=False,
        # Secret-bearing attributes come from Secrets, so the plan masks them.
        store_readable_plan=TerraformV1Alpha2SpecStoreReadablePlan.HUMAN,
        node_selector=_HOME_LAN,
        volumes=[
            TerraformV1Alpha2SpecRunnerPodTemplateSpecVolumes(
                name="cluster-ca",
                config_map=TerraformV1Alpha2SpecRunnerPodTemplateSpecVolumesConfigMap(name=cluster_ca.BUNDLE),
            )
        ],
        volume_mounts=[
            TerraformV1Alpha2SpecRunnerPodTemplateSpecVolumeMounts(
                name="cluster-ca", mount_path=_CA_BUNDLE_DIR, read_only=True
            )
        ],
    )
    add_fleet_rules(chart)
    return chart


def home_switch(
    flux_chart: Chart,
    directory: RenderedDirectory,
    cert_manager: Kustomization,
    external_secrets_operator: Kustomization,
    tofu_controller: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        "monitoring-home-switch",
        directory,
        # Health-checks the Terraform too, so a plan awaiting approval also shows as this
        # Kustomization not Ready.
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(cert_manager, external_secrets_operator, tofu_controller),
    )
