# Cert-Manager Bootstrap Chart

Configures the homelab certificate authority for cert-manager:

- Creates the self-signed bootstrap `ClusterIssuer`
- Mints the long-lived `homelab-ca` `Certificate`
- Publishes the `homelab-ca-issuer` for workloads to reference

Values mirror the previous custom chart defaults; adjust if the namespace, durations, or secret names change.

Install via Helmfile (`cert-manager-bootstrap` release) after the main cert-manager chart.
