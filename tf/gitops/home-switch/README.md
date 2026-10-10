# home-switch

Terraform for the home MikroTik CRS310-8G+2S+ switch (RouterOS 7, `192.168.1.100`): identity,
LAN-only management services with plaintext `api`, `ftp`, `telnet` and `www` disabled, a
read-only `monitoring` user for the RouterOS exporter, and bounded log history on the switch
(10k lines in RAM, 5 x 10k lines rotating on flash) for backfill after an outage.

tofu-controller reconciles it like the other `tf/gitops` modules, from a runner pod pinned to the
home LAN node (`topology.kubernetes.io/zone: home-lan`); the CR and both passwords are generated in
`cluster/cdk8s/monitoring/home_switch.py`. ESO mints:

- `monitoring/home-switch-tofu`: the full-access `tofu` user this module logs in as.
- `monitoring/home-switch-monitoring`: the read-only `monitoring` user this module manages.

Neither password exists outside the cluster and the switch.

cert-manager issues the switch's TLS certificates from the cluster CA (`cluster-internal-ca`), for
`IP:192.168.1.100`, so the Terraform verifies `api-ssl` against the CA bundle mounted into its runner:

- `monitoring/home-switch-tls`: this module imports it for `api-ssl` and `www-ssl`, and imports
  each renewal under a new name before deleting the old one.
- `monitoring/home-switch-bootstrap-tls`: `bootstrap.sh` installs it, so the first reconcile can
  verify the switch.

## Bootstrap (new switch, or after a factory reset)

tofu-controller can't reach the switch until `api-ssl` serves a cluster-CA certificate and the
`tofu` user exists. From a home-LAN host with cluster access (wyrm2), as the switch's admin:

```bash
tf/gitops/home-switch/bootstrap.sh            # [admin-user] [switch-address]
```

The next reconcile (every 15 minutes) applies the rest. To run it now:

```bash
kubectl annotate --overwrite --namespace flux-system terraform/home-switch reconcile.fluxcd.io/requestedAt="$(date +%s)"
```

After a factory reset the admin login is the one on the switch's sticker until you change it.
