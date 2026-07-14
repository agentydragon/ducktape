# Nebula mesh — adding, removing, and re-IPing hosts

The mesh host roster is a single JSON file at the repo root,
`nebula-mesh.json`. Five places read it:

- `nix/nixos/modules/nebula.nix` — derives `lighthouses` + `staticHostMap`
- `nix/nixos/modules/k8s-worker.nix` — derives `controlPlaneEndpoints` (haproxy
  backends for kubelet)
- `cluster/terraform/main/nebula.tf` — per-node ExtensionServiceConfig YAMLs,
  plus a `check {}` block asserting that roster endpoints match the live
  OVH IPs
- `cluster/terraform/main/persistent-auth.tf` — issues per-host Nebula certs
  for every tofu-managed entry
- `cluster/scripts/render_mobile_nebula_config.py` — mobile client config

Pydantic schema and validation: `cluster/scripts/nebula_mesh.py`, exercised by
`//cluster/validation:test_nebula_mesh`.

## Schema cheatsheet

```json
"<host-name>": {
  "nebula_ip":    "10.42.0.N",          // required, IPv4 in 10.42.0.0/16, unique
  "endpoint":     "<ip-or-host>:4242",  // required if lighthouse=true; omit for behind-NAT
  "role":         "control-plane" | "worker" | "laptop" | "non-k8s",
  "lighthouse":   true | false,         // default false
  "relay":        true | false,         // default false
  "managed_by":   "tofu-ovh" | "tofu-proxmox" | "nixos" | "ansible" | "mobile",
  "cert_groups":  [...]                 // optional; embedded in the Nebula cert
}
```

## Add a host

Two flavours depending on how the underlying machine is provisioned.

### OVH Kimsufi (endpoint known before TF apply)

The OVH bare metal IP is allocated when you order the box, so it exists before
any TF apply.

1. Order/activate the OVH server out-of-band — see <kimsufi_provisioning.md>.
2. Edit `nebula-mesh.json`: add the host with `nebula_ip`, `endpoint`, role,
   `lighthouse: true`, `relay: true`, `managed_by: "tofu-ovh"`,
   `cert_groups: ["lighthouse"]`.
3. Add a row to `local.nebula_tf_key_to_host` in `cluster/terraform/main/nebula.tf`.
4. `bazel run //cluster:bootstrap` — `persistent-auth` issues the cert,
   `nebula.tf` builds the per-node config, the drift `check` verifies the
   endpoint matches live OVH data.
5. Restart Nebula on roaming/NixOS hosts (or wait for next `nixos-rebuild
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
   Other hosts don't need to know about behind-NAT hosts.

## Remove a host

### Talos node

1. Cordon + drain in k8s (existing flow).
2. Edit `nebula-mesh.json`: delete the host entry.
3. Remove the matching row from `local.nebula_tf_key_to_host` in nebula.tf
   if applicable.
4. `bazel run //cluster:bootstrap`. TF destroys the underlying resource and
   prunes the cert; remaining Talos nodes get refreshed configs.
5. **Restart Nebula on remaining lighthouses** (e.g., `talosctl service nebula
restart -n <ip>`). Without this, the lighthouses sit in silent
   handshake-timeout loops for the dead peer, which manifests as Chrome /
   client-side NetworkChangeNotifier churn on roaming hosts until the entry
   ages out.
6. `nixos-rebuild switch` on roaming/NixOS hosts to refresh `staticHostMap`
   and `controlPlaneEndpoints`.

### NixOS / laptop / mobile host

1. Edit `nebula-mesh.json`: delete entry.
2. `nixos-rebuild switch` on remaining hosts.
3. Optionally delete `secrets/nebula/<host>.crt` and the SOPS key.
4. CA revocation is out of scope — we don't run an OCSP/CRL flow.

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

TF `check "nebula_mesh_endpoint_drift"` runs at plan time and catches mismatch
against live `data.ovh_dedicated_server.*` IPs.
