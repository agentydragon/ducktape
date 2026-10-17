# home-switch

Terraform for the home MikroTik CRS310-8G+2S+ switch (RouterOS 7, `192.168.1.100`): identity,
LAN management services and a Nebula-only management proxy, with plaintext `api`, `ftp`, `telnet`
and `www` disabled, a
read-only `monitoring` user for the RouterOS exporter, and bounded log history on the switch
(10k lines in RAM, 5 x 10k lines rotating on flash) for backfill after an outage, also
forwarded as BSD syslog over UDP to `alloy-syslog` on optiplex (`192.168.1.10:514`).

[`ports.tf`](ports.tf) labels verified physical connections with interface comments,
which mktxp exports as `mktxp_interface_comment_info`. Interfaces keep their factory
names. The provider's Ethernet resource selects an existing interface by
`factory_name` and updates it; no separate state import is needed. Confirm a moved
connection through `/interface/bridge/host` and the endpoint's MAC before updating
its label. Disconnected and unidentified ports have no managed label.

tofu-controller plans it every 15 minutes, from a runner pod pinned to the home LAN node
(`topology.kubernetes.io/zone: home-lan`), but unlike the other `tf/gitops` modules applies only a
plan a person approves ([Approving a plan](#approving-a-plan)); the CR is generated in
`cluster/cdk8s/monitoring/home_switch.py`. RouterOS accounts:

- `monitoring/home-switch-tofu-password`: the full-access `tofu` user this module logs in as.
  `bootstrap.sh` generates it and writes it to `cluster/k8s/home-switch/tofu-password.sops.yaml`.
- `monitoring/home-switch-monitoring`: the read-only `monitoring` user this module manages, minted
  by ESO.
- The `agentplane` SSH user this module manages has no password; its public key comes from
  `ssh_keys/home-switch-agentplane.pub`.
  Its private key is stored separately in the SOPS-encrypted
  `cluster/k8s/ssh-mcp/keys-agentplane.sops.yaml`, mounted only in the SSH MCP backend.

The `agentplane` account is restricted to `lan_cidr`. Its custom group grants broad configuration
privileges over SSH while denying other login services and RouterOS user management. Agentplane's
staging SSH action still requires a person to approve each command.

## Nebula overlay

The mesh roster gives the switch the Nebula identity `10.42.0.21`. Use that address from a mesh
peer. The ARMv7 Nebula container listens on its Nebula TUN address for TCP ports 22, 443, 8729 and
8291, then opens a separate connection to the RouterOS veth address. RouterOS accepts those
connections only from the container's veth IP. This is a TCP proxy, not an advertised subnet or
forwarded path to `192.168.1.100`, so a peer's local Wi-Fi route for that address stays local.
The Nebula firewall allows those four inbound ports and denies new outbound overlay connections.

The switch's services and full-access `tofu` account allow the home LAN plus the proxy's veth
source address. The read-only monitoring account and Agentplane SSH account remain limited to the
home LAN.

The Nebula host key is stored at `secrets/nebula/home-switch.sops.key`, encrypted to the admin
and cluster SOPS recipients. Its certificate has no unsafe networks. The home-switch ToFu runner
decrypts the key to write the identity files to the USB volume. The RouterOS provider does not mark
`routeros_file.contents` sensitive, so this module disables readable plan ConfigMaps; the Terraform
state still contains the private key.

RouterOS must have its `container` package enabled and its physical Device Mode authorized for
containers. Insert and mount the USB volume before approving the first plan; the module expects
the default `usb1-part1` name and creates `nebula/`, `tmp/`, `layers/` and `root/` on it. The
container's RouterOS `memory-high` threshold is 128 MiB; RouterOS applies reclaim pressure above
that soft limit rather than killing the container at that exact threshold. RouterOS restarts a
failed container after one minute. Its startup script sets `privileged=yes` before starting Nebula
because it needs `/dev/net/tun` and `NET_ADMIN`. Privileged mode
grants the container broad access to the RouterOS host, so keep this image and its Nebula key
trusted. The switch source-NATs the container's Nebula UDP traffic, permits only UDP/4242 egress,
and drops other forwarding from the veth. The TLS certificate includes both `192.168.1.100` for
home-LAN access and `10.42.0.21` for Nebula access.

The switch pulls the ARMv7 image anonymously from GHCR, independent of the cluster's private
Forgejo registry. After CI's first publish, make the `ghcr.io/agentydragon/nebula-routeros`
package public in GitHub's package settings; Flux then replaces the bootstrap `unset` tag with
the CI tag.

cert-manager issues the switch's TLS certificate, `monitoring/home-switch-tls`, from the cluster CA
(`cluster-internal-ca`) for `IP:192.168.1.100` and `IP:10.42.0.21`, so the Terraform verifies
`api-ssl` against the CA bundle mounted into its runner. `bootstrap.sh` installs it for the first
reconcile; this module serves it on `api-ssl` and `www-ssl`, and imports each renewal under a new name before deleting
the old one. cert-manager renews it 30 days before it expires, so approve the plan that installs a
renewal within that window. The switch must hold only that one certificate for its address
(`main.tf` says why); `bootstrap.sh` removes older ones.

## Bootstrap (new switch, or after a factory reset)

tofu-controller can't reach the switch until `api-ssl` serves a cluster-CA certificate and the
`tofu` user exists. From the repository on a home-LAN host with cluster access (wyrm2), as the
switch's admin (SSH asks for its password):

```bash
tf/gitops/home-switch/bootstrap.sh            # [admin-user] [switch-address]
git commit cluster/k8s/home-switch/tofu-password.sops.yaml -m "home-switch: rotate the tofu password"
```

Merge that commit, then approve the next plan, which applies the rest. Until then the committed
password doesn't match the switch, so plans fail at login and nothing is applied. Re-running the
script rotates the password the same way. After a factory reset the admin login is the
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
repeat the plan lookup and patch commands.

To plan now instead of at the next interval:

```bash
kubectl annotate --overwrite --namespace flux-system terraform/home-switch reconcile.fluxcd.io/requestedAt="$(date +%s)"
```
