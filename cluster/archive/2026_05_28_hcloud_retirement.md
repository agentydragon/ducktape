# HCloud Retirement

The cluster previously used Hetzner Cloud (HCloud) VPS nodes in Hillsboro (`hil`)
for Talos control-plane and worker capacity, plus `hcloud-volumes` for a few
durable PVC experiments. This worked, but OVH/Kimsufi is also in HIL and is much
more cost-effective for the always-on cluster footprint, so the cluster moved to
OVH bare metal for Talos nodes.

Useful commit references:

- `2124b0d1f` - added the first OVH KS-5 control-plane node.
- `41ca1de19` - decommissioned `talos-vps-worker-1`.
- `a42cc1e1c` - decommissioned `talos-vps-worker-0` and recorded the Nebula cert
  poisoning postmortem.
- `3fe42e9a3` / `f3078778a` - promoted and deleted the old Hetzner Authentik DB.
- `14f8ec368` / `9eb7f6e48` - promoted and deleted the old Hetzner Grafana DB.
- `f3d222f08` / `92b97bb08` - promoted and deleted the old Hetzner tofu-state DB.
- `b1c57daf5` - cut `tana-mcp` over from `hcloud-volumes` to `local-path-ovh`.
- `26b4b0ba1` - migrated the control plane to OVH and left HCloud scaffolding
  empty.

On 2026-05-28, the last HCloud live resources were manually removed: remaining
orphan volumes, Talos snapshots, the `talos-cluster` firewall, and temporary
debug servers/keys used for inspection. The final orphan PostgreSQL volume was
an old Authentik Bitnami PostgreSQL 17 data directory.

The HCloud token intentionally remains in SOPS in `secrets/shared/cluster-tokens.yaml`
for account-history access and emergency archaeology, but the active cluster
bootstrap, direnv, and OpenTofu config no longer consume it.
