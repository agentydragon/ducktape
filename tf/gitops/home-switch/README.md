# home-switch

Terraform for the home MikroTik CRS310-8G+2S+ switch (RouterOS 7, `192.168.1.100`): identity,
LAN-only management services with plaintext `api`, `ftp`, `telnet` and `www` disabled, a
read-only `monitoring` user for the RouterOS exporter, and bounded log history on the switch
(10k lines in RAM, 5 x 10k lines rotating on flash) for backfill after an outage.

tofu-controller plans it every 15 minutes, from a runner pod pinned to the home LAN node
(`topology.kubernetes.io/zone: home-lan`), but unlike the other `tf/gitops` modules applies only a
plan a person approves ([Approving a plan](#approving-a-plan)); the CR and both passwords are generated in
`cluster/cdk8s/monitoring/home_switch.py`. ESO mints:

- `monitoring/home-switch-tofu`: the full-access `tofu` user this module logs in as.
- `monitoring/home-switch-monitoring`: the read-only `monitoring` user this module manages.

Neither password exists outside the cluster and the switch.

cert-manager issues the switch's TLS certificates from the cluster CA (`cluster-internal-ca`), for
`IP:192.168.1.100`, so the Terraform verifies `api-ssl` against the CA bundle mounted into its runner:

- `monitoring/home-switch-tls`: this module imports it for `api-ssl` and `www-ssl`, and imports
  each renewal under a new name before deleting the old one. cert-manager renews it 30 days
  before it expires, so approve the plan that installs a renewal within that window.
- `monitoring/home-switch-bootstrap-tls`: `bootstrap.sh` installs it, so the first reconcile can
  verify the switch.

## Bootstrap (new switch, or after a factory reset)

tofu-controller can't reach the switch until `api-ssl` serves a cluster-CA certificate and the
`tofu` user exists. From a home-LAN host with cluster access (wyrm2), as the switch's admin:

```bash
tf/gitops/home-switch/bootstrap.sh            # [admin-user] [switch-address]
```

Then approve the next plan, which applies the rest. After a factory reset the admin login is the
one on the switch's sticker until you change it.

## Approving a plan

A plan with changes (drift, or a change merged here) leaves the `Terraform`, and so the
`monitoring-home-switch` Kustomization, not Ready, waiting on `spec.approvePlan` naming that plan:

```bash
kubectl get terraform --namespace flux-system home-switch   # READY False/Unknown: a plan is pending
kubectl get configmap --namespace flux-system tfplan-default-home-switch --output jsonpath='{.data.tfplan}'
plan=$(kubectl get terraform --namespace flux-system home-switch --output jsonpath='{.status.plan.pending}')
kubectl patch terraform --namespace flux-system home-switch --field-manager=home-switch-approval \
  --type merge --patch "{\"spec\":{\"approvePlan\":\"$plan\"}}"
```

The approval is a live patch rather than a commit: a plan's id names the `ducktape` revision it was
planned at, which every merge to `devel` moves, including the one that would approve it. The
generator leaves `approvePlan` unset, so Flux keeps the patch, and it approves only that one plan; if a merge replans in between,
re-run the last two commands.

To plan now instead of at the next interval:

```bash
kubectl annotate --overwrite --namespace flux-system terraform/home-switch reconcile.fluxcd.io/requestedAt="$(date +%s)"
```
