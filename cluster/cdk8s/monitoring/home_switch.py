"""The home MikroTik switch (CRS310): its api-ssl/www-ssl certificates, issued by cert-manager from
the cluster CA so clients verify the switch against the CA bundle.

- `home-switch-tls`: the certificate the switch serves.
- `home-switch-bootstrap-tls`: installed by hand after a factory reset, so the first connection
  from the cluster can verify the switch.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart

from cluster.cdk8s.cert_manager import cluster_ca
from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.cert_manager.certificate import Certificate, CertificatePrivateKey

NAME = "home-switch"
NAMESPACE = "monitoring"
OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/{NAME}"
TLS_SECRET = "home-switch-tls"
BOOTSTRAP_TLS_SECRET = "home-switch-bootstrap-tls"
# The switch's DHCP lease, which its certificates name.
_SWITCH_ADDRESS = "192.168.1.100"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    for id, secret_name in (("tls", TLS_SECRET), ("bootstrap-tls", BOOTSTRAP_TLS_SECRET)):
        Certificate(
            chart,
            id,
            metadata=ApiObjectMetadata(name=secret_name, namespace=NAMESPACE),
            secret_name=secret_name,
            issuer_ref=cluster_ca.INTERNAL_ISSUER,
            # The switch's identity name.
            common_name="CRS310",
            ip_addresses=[_SWITCH_ADDRESS],
            private_key=CertificatePrivateKey.ecdsa_p256(),
        )
    add_fleet_rules(chart)
    return chart


def home_switch(flux_chart: Chart, directory: RenderedDirectory, cert_manager: Kustomization) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        "monitoring-home-switch",
        directory,
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(cert_manager),
    )
