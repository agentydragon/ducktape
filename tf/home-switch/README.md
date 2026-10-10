# home-switch

OpenTofu root for the home MikroTik CRS310-8G+2S+ switch (RouterOS 7, `192.168.1.100`):
identity, a self-signed api-ssl/www-ssl certificate, LAN-only management services, and a
read-only `monitoring` user for the RouterOS exporter.

Operator-run from a host on the home LAN (wyrm2), not reconciled by tofu-controller. State is
in the `home_switch` schema of the cluster's tofu-state database.

## Credentials

- **Admin login**: `secrets/shared/home-switch-admin.yaml` (SOPS), keys `username` and
  `password`. Matches the switch's own admin account; this root does not manage it.
- **`monitoring` password**: minted by ESO into Secret `monitoring/home-switch-monitoring`
  (`cluster/cdk8s/monitoring/home_switch.py`). This root reads it and sets it on the switch,
  so it never exists outside the cluster and the switch.

Create or update the admin secret from the repo root, inside the repo direnv:

```bash
sops secrets/shared/home-switch-admin.yaml
```

## Apply

```bash
cd tf/home-switch   # direnv loads PG_CONN_STR and the kubeconfig
tofu init
tofu apply
```

## Bootstrap (first apply, or after a reset/reflash)

A factory-fresh switch has plain `api` (8728) enabled and no certificate for `api-ssl`. A
reset also restores the admin password, so first make the switch's admin login match the
SOPS file (WebFig or `ssh admin@192.168.1.100`, `/user set admin password=...`). Then:

```bash
tofu apply -var hosturl=api://192.168.1.100:8728 -target='routeros_ip_service.tls["api-ssl"]'
tofu apply
```

The first apply creates the certificate and enables `api-ssl` over the plaintext API; the
second runs over `api-ssl` and disables plaintext `api`, `ftp`, `telnet` and `www`.

If the switch comes back on another DHCP address, pass `-var hosturl=apis://<address>:8729`
(and the `api://` form for the bootstrap step).
