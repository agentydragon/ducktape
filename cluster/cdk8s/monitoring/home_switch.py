"""The home MikroTik switch (CRS310): the `tf/gitops/home-switch` Terraform, and what it installs
on the switch.

RouterOS accounts:

- `tofu`: the full-access user the Terraform logs in as. `tf/gitops/home-switch/bootstrap.sh`
  creates it on the switch with a fresh password, which it writes to this directory's
  `tofu-password.sops.yaml`; re-running it rotates the password.
- `monitoring`: the read-only user the Terraform manages, which the MKTXP exporter logs in as.
  ESO mints it.
- `agentplane`: the SSH user whose public key Tofu installs for Agentplane's SSH MCP backend. Its
  RouterOS password is empty; its private key is separately SOPS-encrypted and mounted into the
  backend.

cert-manager issues the switch's api-ssl/www-ssl certificate, `home-switch-tls`, from the cluster CA,
so clients verify it against the CA bundle: `bootstrap.sh` installs it for the Terraform's first
connection, under the name the Terraform gives it, and the Terraform installs each renewal.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cert_manager_crds.io.cert_manager import Certificate, CertificateSpec
from external_secrets_crds.io.external_secrets import ExternalSecretSpecTargetCreationPolicy
from pydantic import BaseModel, ConfigDict, Field
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts
from tofu_controller.io.fluxcd.contrib.infra import (
    TerraformSpecRunnerPodTemplateSpecVolumeMounts,
    TerraformSpecRunnerPodTemplateSpecVolumes,
    TerraformSpecRunnerPodTemplateSpecVolumesConfigMap,
    TerraformSpecStoreReadablePlan,
)

from cluster.cdk8s import terraform
from cluster.cdk8s.cert_manager import cluster_ca
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.home_lan import HOME_LAN
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.monitoring import alloy
from cluster.cdk8s.monitoring.namespace import NAMESPACE
from cluster.cdk8s.providers.cert_manager.certificate import CertificatePrivateKey
from cluster.cdk8s.secret_ref import SecretKey, SecretRef

NAME = "home-switch"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}"
# Written by tf/gitops/home-switch/bootstrap.sh, which names the Secret `home-switch-tofu-password`.
TOFU_PASSWORD_FILE = "tofu-password.sops.yaml"
# tf/gitops/home-switch/main.tf and bootstrap.sh read these Secrets and keys by name.
MONITORING_PASSWORD = SecretRef(namespace=NAMESPACE, name="home-switch-monitoring").key("password")
TLS_SECRET = "home-switch-tls"
# The switch is reachable only from the home LAN; OptiPlex is the cluster node on it.
_HOME_LAN = {"topology.kubernetes.io/zone": "home-lan"}
# main.tf's provider `ca_certificate` reads the bundle here.
_CA_BUNDLE_DIR = "/etc/cluster-ca"


class HomeSwitchVars(BaseModel):
    """The inputs of tf/gitops/home-switch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    switch_address: str = Field(description="The switch's management address, which its certificate names.")
    agentplane_ssh_public_key: str = Field(description="Public SSH key Tofu installs for the Agentplane user.")
    lan_cidr: str = Field(
        description="The only network management services and the monitoring user accept connections from."
    )
    syslog_address: str = Field(description="Where the switch sends its log as BSD syslog over UDP.")
    syslog_port: int = Field(description="The UDP port at `syslog_address`.")
    netwatch_targets: dict[str, str] = Field(description="Named LAN addresses probed by the switch itself.")


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


def chart(app: App, module: ArtifactGeneratorSpecArtifacts, agentplane_ssh_public_key: str) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    _mint(
        chart,
        "monitoring-password",
        MONITORING_PASSWORD,
        "Password of the home switch's read-only RouterOS user; tf/gitops/home-switch sets it on the switch.",
    )
    Certificate(
        chart,
        "tls",
        metadata=ApiObjectMetadata(name=TLS_SECRET, namespace=NAMESPACE),
        spec=CertificateSpec(
            secret_name=TLS_SECRET,
            issuer_ref=cluster_ca.INTERNAL_ISSUER,
            # main.tf's routeros_system_certificate names the same common name.
            common_name="CRS310",
            # The address tf/gitops/home-switch connects to.
            ip_addresses=[str(HOME_LAN.switch)],
            private_key=CertificatePrivateKey.rsa_2048(),
        ),
    )
    terraform.gitops_terraform(
        chart,
        "terraform",
        module=module,
        variables=HomeSwitchVars(
            switch_address=str(HOME_LAN.switch),
            agentplane_ssh_public_key=agentplane_ssh_public_key,
            lan_cidr=str(HOME_LAN.network),
            # Where alloy-syslog listens.
            syslog_address=str(HOME_LAN.optiplex),
            syslog_port=alloy.SYSLOG_HOST_PORT,
            netwatch_targets={"att-gateway": str(HOME_LAN.gateway), "optiplex-lan": str(HOME_LAN.optiplex)},
        ),
        # Plans every interval and reports drift; a person approves each apply (README.md).
        auto_apply=False,
        # Secret-bearing attributes come from Secrets, so the plan masks them.
        store_readable_plan=TerraformSpecStoreReadablePlan.HUMAN,
        node_selector=_HOME_LAN,
        volumes=[
            TerraformSpecRunnerPodTemplateSpecVolumes(
                name="cluster-ca", config_map=TerraformSpecRunnerPodTemplateSpecVolumesConfigMap(name=cluster_ca.BUNDLE)
            )
        ],
        volume_mounts=[
            TerraformSpecRunnerPodTemplateSpecVolumeMounts(name="cluster-ca", mount_path=_CA_BUNDLE_DIR, read_only=True)
        ],
    )
    return chart
