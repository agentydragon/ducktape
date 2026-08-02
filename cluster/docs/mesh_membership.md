# Nebula mesh — adding, removing, and re-IPing hosts

The mesh host roster is a single JSON file at the repo root,
`nebula-mesh.json`. Six places read it:

- `nix/nixos/modules/nebula.nix` — derives `lighthouses`, `staticHostMap`, and
  peer MTU routes
- `nix/nixos/modules/k8s-worker.nix` — derives `controlPlaneEndpoints` (haproxy
  backends for kubelet)
- `cluster/terraform/main/nebula.tf` — per-node ExtensionServiceConfig YAMLs,
  per-node MTU route patches, plus a `check {}` block asserting that roster
  endpoints match the live OVH IPs
- `cluster/terraform/main/persistent-auth.tf` — reads persisted per-host Nebula
  certificate material for every tofu-managed entry
- `ansible/roles/nebula` — renders Atlas's config and peer MTU routes
- `cluster/scripts/render_mobile_nebula_config.py` — mobile client config

Pydantic schema and validation: `cluster/scripts/nebula_mesh.py`, exercised by
`//cluster/validation:test_nebula_mesh`.

## Roaming k8s nodes

A `role: "laptop"` entry is also a roaming Kubernetes node, and its count sets
the floor for `maxUnavailable` on every DaemonSet that schedules there — an
offline node's pod holds the unavailable budget forever, deadlocking the rollout
while Helm and Flux report success.
`//cluster/validation:test_roaming_daemonset_capacity` derives the count from
this roster and fails with the files to fix; the incident is in
<lessons_learned/2026_07_31_promtail_daemonset_roaming_deadlock.md>.

## Schema cheatsheet

```json
"<host-name>": {
  "nebula_ip":    "10.42.0.N",          // required, IPv4 in 10.42.0.0/16, unique
  "endpoint":     "<ip-or-host>:4242",  // required if lighthouse=true; omit for behind-NAT
  "role":         "control-plane" | "worker" | "laptop" | "non-k8s",
  "lighthouse":   true | false,         // default false
  "relay":        true | false,         // default false
  "managed_by":   "tofu-ovh" | "tofu-proxmox" | "tofu-home" | "nixos" | "ansible" | "mobile",
  "cert_groups":  [...],                // optional; embedded in the Nebula cert
  "destination_mtu": 1100               // optional; MTU all peers use when sending to this host
}
```

`destination_mtu` is a per-destination exception to the mesh-wide Nebula TUN
MTU of 1420. Set it only when the named host's underlay cannot reliably carry
the normal encrypted packet size; omit the field rather than setting it to
`null` when no exception applies. Other hosts install an exact `/32` route to
that destination, while the constrained host uses the same MTU toward all
peers. This makes managed Linux paths symmetric without changing traffic
between other managed Linux pairs. Mobile Nebula's global fallback is the
deliberate exception: all of that mobile client's mesh traffic uses the
smallest declared constraint.

## Add a host

Two flavours depending on how the underlying machine is provisioned.

### OVH Kimsufi (endpoint known before TF apply)

The OVH bare metal IP is allocated when you order the box, so it exists before
any TF apply.

1. Order/activate the OVH server out-of-band — see <kimsufi_provisioning.md>.
2. Edit `nebula-mesh.json`: add the host with `nebula_ip`, `endpoint`, role,
   `lighthouse: true`, `relay: true`, `managed_by: "tofu-ovh"`,
   `cert_groups: ["lighthouse"]`.
3. Add the host to `local.kimsufi_servers` in
   `cluster/terraform/main/ovh-nodes.tf`; the Terraform-managed Nebula host set
   is derived from that inventory.
4. Generate and persist `secrets/nebula/<host>.crt` and
   `secrets/nebula/<host>.sops.key` with the exact FQDN, Nebula IP, and groups
   from the new roster entry; see <secrets.md> "Generating a new cert".
5. `bazel run //cluster:bootstrap` — `nebula.tf` builds the per-node config,
   and the drift `check` verifies the endpoint matches live OVH data.
6. Restart Nebula on roaming/NixOS hosts (or wait for next `nixos-rebuild
switch`) so they pick up the new `static_host_map`.

### NixOS / Ansible / laptop / mobile (manual cert)

For nodes not provisioned by TF: `wyrm2`/`rugged`/`iguana` (`nixos`), `atlas`
(`ansible` — Debian/Proxmox, provisioned by `ansible/atlas.yaml`), `pixel6`
(`mobile`).

1. Generate the cert under `secrets/nebula/<host>.crt` /
   `secrets/nebula/<host>.sops.key` — see <secrets.md> "Nebula Certs for
   Non-Talos Nodes".
2. Edit `nebula-mesh.json`: add the host with `nebula_ip` (matching the cert),
   role, `managed_by: "nixos"` / `"ansible"` / `"mobile"`. No endpoint, no
   lighthouse (unless you really mean to serve as one).
3. Deploy on the host — `nixos-rebuild switch` (nixos),
   `ansible-playbook <host>.yaml --tags nebula` (ansible), or mobile import.
   Other hosts don't need to know about an ordinary behind-NAT host. If the
   entry sets `destination_mtu`, redeploy every managed route-policy consumer:
   apply the Talos Terraform configuration, rebuild the NixOS peers, and run
   the Nebula role on Atlas. Also regenerate and re-import each Mobile Nebula
   configuration; mobile uses the smallest declared constraint as its global
   TUN MTU because the platform does not expose per-route MTUs.

### Home bare-metal Talos worker

Home bare-metal Talos workers are defined in
`cluster/terraform/main/home-nodes.tf`. They have no public Nebula endpoint and
are neither lighthouses nor relays.

1. Add the host to `local.home_nodes` with a stable install-disk path, Nebula
   IP, and home topology labels.
2. Add the matching roster entry with `role: "worker"` and
   `managed_by: "tofu-home"`.
3. Apply the targeted cert/config dependencies, then deliver the generated
   machine configuration to the node's LAN maintenance address. See
   <optiplex_provisioning.md> for the first home bare-metal example.

## Remove a host

### Talos node

1. Cordon + drain in k8s (existing flow).
2. Edit `nebula-mesh.json`: delete the host entry.
3. Remove the matching inventory entry from `local.kimsufi_servers` (or the
   legacy `local.kimsufi_cp_servers`) in `cluster/terraform/main/ovh-nodes.tf`.
4. `bazel run //cluster:bootstrap`. TF destroys the underlying resource and
   refreshes remaining Talos node configs.
5. Delete `secrets/nebula/<host>.crt` and
   `secrets/nebula/<host>.sops.key` once no surviving node configuration refers
   to the removed identity.
6. **Restart Nebula on remaining lighthouses** (e.g., `talosctl service nebula
restart -n <ip>`). Without this, the lighthouses sit in silent
   handshake-timeout loops for the dead peer, which manifests as Chrome /
   client-side NetworkChangeNotifier churn on roaming hosts until the entry
   ages out.
7. `nixos-rebuild switch` on roaming/NixOS hosts to refresh `staticHostMap`
   and `controlPlaneEndpoints`.
8. Run the Nebula role on Atlas if the removed host had `destination_mtu`.

### NixOS / laptop / mobile host

1. Edit `nebula-mesh.json`: delete entry.
2. If the entry had `destination_mtu`, apply the Talos Terraform
   configuration, rebuild the remaining NixOS peers, and run the Nebula role
   on Atlas so every reciprocal route is removed. Regenerate and re-import
   Mobile Nebula configurations so their global fallback is recalculated.

3. Otherwise, rebuild only hosts that consumed an endpoint or lighthouse
   setting from the removed entry.
4. Optionally delete `secrets/nebula/<host>.crt` and the SOPS key.
5. CA revocation is out of scope — we don't run an OCSP/CRL flow.

## Provider re-IPs a host

OVH reallocates a Kimsufi IP:

1. `bazel run //cluster:bootstrap` (or any `tofu plan`) fails the drift
   `check` with a diff: `roster=<old> live=<new>`.
2. Update `endpoint` in `nebula-mesh.json` to match.
3. Re-apply. The new `static_host_map` propagates.
4. Restart Nebula on lighthouses + roaming hosts.

## Validation

`bazel test //cluster/validation:test_nebula_mesh` enforces:

- Schema: every host has `nebula_ip`, valid `role`, `managed_by`.
- IP validity + uniqueness within `10.42.0.0/16`.
- Endpoints parse as `host:port`.
- `lighthouse: true` implies `endpoint` set.
- At least two reachable lighthouses (no SPOF for roaming clients).
- At least one `control-plane` (otherwise `controlPlaneEndpoints` would be
  empty).
- `destination_mtu`, when set, is between Nebula's 500-byte route minimum and
  the mesh TUN MTU of 1420.

TF `check "nebula_mesh_endpoint_drift"` runs at plan time and catches mismatch
against live `data.ovh_dedicated_server.*` IPs.
