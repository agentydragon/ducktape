# Nebula mesh: single source of truth for host roster

Date: 2026-05-25
Author: rai (with claude)

## Problem

The Nebula host roster is duplicated across at least five places that drift
independently:

| Location                                                             | What it stores                                               | Drift seen                                                      |
| -------------------------------------------------------------------- | ------------------------------------------------------------ | --------------------------------------------------------------- |
| `nebula-mesh.json`                                                   | `lighthouse_ips` + `static_host_map`                         | Lists `.11` (NotReady,SchedulingDisabled) as lighthouse         |
| `cluster/terraform/main/nebula.tf` (`nebula_node_names`)             | Cert subject ↔ TF key mapping (6 entries)                    | Re-asserted in two more places                                  |
| `cluster/terraform/main/nebula.tf` (`nebula_static_host_map`)        | Derived from `hcloud_server.*` + `data.ovh_dedicated_server` | Can drift vs `nebula-mesh.json` (no check)                      |
| `cluster/terraform/main/persistent-auth.tf` (`talos_nebula_nodes`)   | Per-node `{ ip, groups }` for cert issuance                  | 6 entries, hand-maintained                                      |
| `nix/nixos/modules/k8s-worker.nix` (`controlPlaneEndpoints` default) | Hardcoded `[.1, .2, .10]`                                    | `.10` is currently `NotReady,SchedulingDisabled`; `.15` missing |

Concrete failure (2026-05-25): rugged's haproxy still loadbalances to a dead CP
because the `k8s-worker.nix` default never learned about `.15`. Kubelet on
rugged stayed in EOF retries even after Nebula was restarted.

## Goal

A single roster file describes every Nebula host. All five consumers derive
their views from it. Adding or removing a machine is one edit + the usual
apply, with no second edit forced by drift between layers.

## Schema

`nebula-mesh.json`:

```json
{
  "_comment": "SSOT for Nebula mesh. Consumers: nebula.nix, k8s-worker.nix, persistent-auth.tf, nebula.tf, render_mobile_nebula_config.py.",
  "hosts": {
    "talos-vps-cp-0": {
      "nebula_ip": "10.42.0.1",
      "endpoint": "5.78.142.158:4242",
      "role": "control-plane",
      "lighthouse": true,
      "relay": true,
      "managed_by": "tofu-hcloud",
      "cert_groups": ["lighthouse"]
    },
    "talos-vps-cp-1": {
      "nebula_ip": "10.42.0.2",
      "endpoint": "5.78.144.197:4242",
      "role": "control-plane",
      "lighthouse": true,
      "relay": true,
      "managed_by": "tofu-hcloud",
      "cert_groups": ["lighthouse"]
    },
    "talos-pve-cp-0": { "nebula_ip": "10.42.0.10", "role": "control-plane", "managed_by": "tofu-proxmox" },
    "talos-vps-worker-0": {
      "nebula_ip": "10.42.0.11",
      "endpoint": "5.78.106.249:4242",
      "role": "worker",
      "lighthouse": true,
      "relay": true,
      "managed_by": "tofu-hcloud",
      "cert_groups": ["lighthouse"]
    },
    "talos-kimsufi-worker-0": {
      "nebula_ip": "10.42.0.13",
      "endpoint": "147.135.39.162:4242",
      "role": "worker",
      "lighthouse": true,
      "relay": true,
      "managed_by": "tofu-ovh",
      "cert_groups": ["lighthouse"]
    },
    "talos-kimsufi-worker-1": {
      "nebula_ip": "10.42.0.14",
      "endpoint": "147.135.39.176:4242",
      "role": "worker",
      "lighthouse": true,
      "relay": true,
      "managed_by": "tofu-ovh",
      "cert_groups": ["lighthouse"]
    },
    "talos-kimsufi-cp-0": {
      "nebula_ip": "10.42.0.15",
      "endpoint": "147.135.37.175:4242",
      "role": "control-plane",
      "lighthouse": true,
      "relay": true,
      "managed_by": "tofu-ovh",
      "cert_groups": ["lighthouse"]
    },
    "wyrm2": { "nebula_ip": "10.42.0.20", "role": "worker", "managed_by": "nixos" },
    "rugged": { "nebula_ip": "10.42.0.30", "role": "laptop", "managed_by": "nixos" }
  }
}
```

Field rules:

- `nebula_ip` (required): the mesh IP, no mask.
- `role` (required): one of `control-plane`, `worker`, `laptop`. Drives
  `controlPlaneEndpoints` and per-host scheduling decisions.
- `endpoint` (optional): `host:port` reachable from the public internet. Must
  be present if `lighthouse: true`. Absent for behind-NAT hosts.
- `lighthouse` (default `false`): is this a Nebula lighthouse for others.
- `relay` (default `false`): does this host relay for NAT'd peers.
- `managed_by` (required): one of `tofu-hcloud`, `tofu-ovh`, `tofu-proxmox`,
  `nixos`, `mobile`. Drives which TF data source (if any) supplies the live
  endpoint for drift checks, and which cert flow issues the host cert.
- `cert_groups` (optional, default `[]`): groups to embed in the Nebula cert.

### Projections (derived where consumed)

```text
lighthouse_ips         = [h.nebula_ip for h in hosts.values() if h.lighthouse]
static_host_map        = {h.nebula_ip: [h.endpoint] for h in hosts.values() if h.endpoint}
control_plane_endpoints = [f"{h.nebula_ip}:6443" for h in hosts.values() if h.role == "control-plane"]
talos_cert_nodes       = {f"{name}.nebula.allegedly.works": {ip: f"{h.nebula_ip}/16", groups: …}
                          for name, h in hosts.items() if h.managed_by.startswith("tofu-")}
```

## File changes

1. `nebula-mesh.json` — new schema (above).
2. `nix/nixos/modules/nebula.nix` — derive `lighthouses` and `staticHostMap`
   options from `hosts` map (~10 LOC delta).
3. `nix/nixos/modules/k8s-worker.nix` — change `controlPlaneEndpoints` default
   from the hardcoded triple to a derivation filtered on `role ==
"control-plane"` (~6 LOC delta).
4. `cluster/terraform/main/nebula.tf` — replace `nebula_node_names`,
   `nebula_static_host_map`, `nebula_lighthouse_ips` with derivations from the
   new schema. Per-node `nebula_configs` map keeps its current shape but its
   per-node lighthouse/relay booleans now read from the host record.
5. `cluster/terraform/main/persistent-auth.tf` — replace `talos_nebula_nodes`
   with a derivation from the hosts map filtered on `managed_by ~
"^tofu-.*"`.
6. `cluster/terraform/main/nebula.tf` — add `check "nebula_mesh_matches_live"`
   asserting roster endpoints match live `hcloud_server.*` /
   `data.ovh_dedicated_server.*` IPs.
7. `cluster/scripts/render_mobile_nebula_config.py` — `~10` LOC delta to
   derive `lighthouses` + `static_host_map` from the hosts map.
8. New `cluster/validation/test_nebula_mesh.py` — schema validation + invariant
   checks (see Validation below).

## Endpoint resolution policy (chicken-and-egg story)

The single subtle point. Endpoints fall into three classes:

| Class                       | Hosts                                     | When endpoint is known                         | Lives in roster       |
| --------------------------- | ----------------------------------------- | ---------------------------------------------- | --------------------- |
| Stable, externally-assigned | OVH Kimsufi, laptops with stable upstream | At provider order time, never reassigned by us | Yes, baked            |
| TF-created, post-apply      | Hetzner VPS (`hcloud_server`)             | After `tofu apply` creates the resource        | Yes, baked once known |
| Behind NAT                  | Proxmox CP, roaming laptops               | Not applicable (no inbound endpoint)           | Omitted               |

The TF `check {}` block enforces:

- For every host in the roster with `endpoint` set and `managed_by =
tofu-hcloud`, the roster endpoint must equal `hcloud_server[name].ipv4_address:4242`.
- For every host with `managed_by = tofu-ovh`, the roster endpoint must equal
  `data.ovh_dedicated_server[name].ip:4242`.
- For every TF-created or TF-data-sourced host, the roster MUST list it.

Drift outcomes:

- OVH reallocates an IP for a bare-metal node → `tofu plan` fails the check
  with a clear diff. Fix: bump the roster endpoint, commit, re-apply.
- Hetzner destroys/recreates a VPS → same.
- New Hetzner CP added: see "Add a machine" below for the explicit
  two-step.

## Operations

### Add a machine

**Existing OVH Kimsufi node** (endpoint known before TF apply):

1. Order/activate the node out-of-band (existing flow).
2. Edit `nebula-mesh.json`: add a host entry with `nebula_ip`, `endpoint`,
   role, etc.
3. `bazel run //cluster:bootstrap`. persistent-auth issues the cert,
   infrastructure rolls the per-node machine config to existing nodes.
4. Restart Nebula on roaming/NixOS hosts (or wait for `nixos-rebuild switch`)
   so they pick up the new static_host_map.

**New Hetzner VPS** (endpoint only known after TF creates it):

1. Edit `nebula-mesh.json`: add the host with `nebula_ip`, role, flags,
   `managed_by: tofu-hcloud`, **and omit `endpoint` initially**. Mark with a
   comment that the endpoint will be filled in after first apply.
2. `bazel run //cluster:bootstrap`:
   - persistent-auth issues the cert (cert only needs `nebula_ip`).
   - infrastructure provisions the `hcloud_server`.
   - `check` block runs; passes because rule only fires when `endpoint` is
     set in the roster.
3. `tofu output nebula_endpoint_for[talos-vps-cp-N]` (we add this output as
   part of the migration) prints the new IP.
4. Edit `nebula-mesh.json`: fill in the now-known `endpoint`.
5. `bazel run //cluster:bootstrap` again. Now the `check` enforces match.
   Other Talos nodes get the updated `static_host_map` via per-node
   `ExtensionServiceConfig`.
6. `nixos-rebuild switch` on roaming hosts.

This two-step is explicit and obvious. Alternative considered (TF writes a
generated lock file): rejected because it couples Nix builds to TF apply
output and adds a generated artifact to manage.

**NixOS/laptop host** (e.g., a new laptop):

1. Edit `nebula-mesh.json`: add entry with `nebula_ip`, role, `managed_by:
nixos`, no endpoint, no lighthouse.
2. Generate cert via existing manual flow (`secrets/nebula/<host>.crt`
   committed, `<host>.sops.key` encrypted).
3. `home-manager`/`nixos-rebuild switch` on the new host. It reads the
   shared `nebula-mesh.json` for lighthouse and static_host_map.

No chicken-and-egg here: the new host doesn't need to be advertised to
others (it has no endpoint, won't be discovered, NATs out).

### Remove a machine

**Talos node**:

1. Cordon + drain in k8s (existing flow).
2. Edit `nebula-mesh.json`: delete the host entry.
3. `bazel run //cluster:bootstrap`:
   - infrastructure destroys the `hcloud_server` / OVH detach.
   - persistent-auth: cert artifact pruned (per-host
     `null_resource.nebula_node_cert` is gone, so its `local_file`s are
     destroyed).
   - Other Talos nodes get refreshed machine configs without the dead peer.
4. Restart Nebula on remaining lighthouses to evict the stale handshake
   state immediately (otherwise it drains via timeout — what bit us today).
5. Roaming/NixOS hosts: `nixos-rebuild switch` to refresh
   `staticHostMap`/`controlPlaneEndpoints`.

**Lighthouse-removal safety**: the validation test (below) fails if removing
the host would leave fewer than 2 lighthouses reachable from any roaming-class
host. Forces an explicit override or a phased removal (add replacement first).

**NixOS/laptop host**:

1. Edit `nebula-mesh.json`: delete entry.
2. `nixos-rebuild switch` on remaining hosts.
3. Optionally `sops` -d / remove the cert files from `secrets/nebula/`.
4. CA revocation: out of scope (we don't run an OCSP / CRL flow today).

### Provider re-IPs a host

OVH reallocates Kimsufi IP, or hcloud destroys/recreates a VPS:

1. `tofu plan` from any change fails the `check` with a diff showing
   `roster=<old> live=<new>`.
2. Bump the `endpoint` in `nebula-mesh.json`.
3. `bazel run //cluster:bootstrap` to roll the new static_host_map.
4. Restart Nebula on lighthouses + roaming hosts.

## Validation

A single Bazel-runnable test (`cluster/validation/test_nebula_mesh.py`)
covering:

- Schema: every host has `nebula_ip` (CIDR-less /16), `role` in the enum,
  `managed_by` in the enum.
- Uniqueness: `nebula_ip` unique across hosts.
- `lighthouse: true` ⇒ `endpoint` present.
- `lighthouse: true` ⇒ `relay: true` (we have no reason to run a non-relaying
  lighthouse today; flip to a warning if that changes).
- At least 2 hosts with `lighthouse: true` AND `endpoint` reachable from a
  laptop (lighthouse redundancy invariant).
- At least 1 host with `role: control-plane` reachable to a Talos node
  (for `controlPlaneEndpoints`).
- Cross-check with `persistent-auth.tf` cert directory:
  `secrets/nebula/<host>.crt` must exist for every non-tofu-managed host
  (i.e., the manual cert flow has produced it).

TF `check` block (separate, runtime) covers live-vs-roster endpoint drift.

## Migration from current state

Single PR. Touches:

- `nebula-mesh.json` (replace contents)
- `nix/nixos/modules/nebula.nix`
- `nix/nixos/modules/k8s-worker.nix`
- `cluster/terraform/main/nebula.tf`
- `cluster/terraform/main/persistent-auth.tf`
- `cluster/scripts/render_mobile_nebula_config.py`
- `cluster/validation/BUILD.bazel` + `test_nebula_mesh.py`

Order of land:

1. Land PR. CI runs the validation test against the new roster.
2. Locally: `bazel run //cluster:bootstrap`. TF refreshes Talos machine
   configs and rolls per-node Nebula configs; the check block is satisfied
   because we baked existing endpoints into the new roster.
3. `nixos-rebuild switch` on rugged, wyrm2 (and iguana next time it's online).
   This is what would have fixed today's incident.

Backwards compatibility: none required (single repo, monorepo policy
forbids transitional shims).

## Out of scope

- Nebula CA rotation (existing manual flow, not affected).
- Nebula cert renewal automation (separate plan).
- Mobile client provisioning UX (renderer is updated; UX unchanged).
- Per-host firewall rules (everything currently allows any/any; if we add
  group-based rules later, `cert_groups` is already wired through).

## Documentation

A new `cluster/docs/mesh_membership.md` documents the add / remove / re-IP
flows above (one runbook each, ~30 lines total). The plan above is the design
record; the runbook is the day-to-day reference.

Cross-links to add in the same PR:

- `cluster/AGENTS.md` — under "Key Files", add a row for `nebula-mesh.json`
  with one-line description and pointer to `<docs/mesh_membership.md>`.
- `cluster/README.md` — under "Node Types", add "See
  <docs/mesh_membership.md> to add or remove a node."
- `cluster/docs/kimsufi_provisioning.md` — final step already says "add to
  TF"; change to "follow <mesh_membership.md> step 2" once landed.
- `nebula-mesh.json` `_comment` field — point at the runbook.

## File format

YAML was considered for hand-editability (comments, no trailing-comma
strictness). Rejected: Nix has no pure YAML reader, and this repo uses
`builtins.fromJSON` exclusively. The only options that keep Nix happy are
(a) IFD via `yq` (slows every eval, discouraged) or (b) committing a
generated `.json` alongside a YAML source (risks edit-the-wrong-file). For a
~10-host roster the comment-loss is not worth either tradeoff. Stay on JSON;
move the `_comment` field's content into the runbook doc instead.

## Open questions

- Should the roster also encode `topology.kubernetes.io/region` and replace
  the per-machineconfig `nodeLabels` block? Currently regions live in Talos
  machineconfig patches and `k8s-worker.nix` host configs. Probably yes
  in a follow-up — not part of this plan.
