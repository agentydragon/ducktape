# Provisioning an OVH Kimsufi node

Add a Kimsufi bare-metal node in OVH HIL to the cluster. Talos is installed via
OVH rescue boot → `dd` of the metal image; then we apply a Talos machine config
that joins the cluster and brings up the Nebula extension.

The Terraform code in `cluster/terraform/main/ovh-nodes.tf` declares KS-5 and
KS-GAME slots keyed by `for_each` over `local.active_kimsufi_servers` and
`local.active_kimsufi_cp_servers`. A slot is "active" iff its `service_name`
variable is non-empty.

## Prerequisites

- OVH Kimsufi server purchased, `state=ok`. Verify:
  ```bash
  python3 -c '
  import hashlib, json, time, urllib.request, yaml
  c=yaml.safe_load(open("/tmp/claude/ovh-creds.yaml"))  # decrypted earlier
  url="https://api.us.ovhcloud.com/1.0/dedicated/server/<NAME>"
  with urllib.request.urlopen("https://api.us.ovhcloud.com/1.0/auth/time") as r:
      dt=int(r.read())-int(time.time())
  ts=str(int(time.time())+dt)
  sig="$1$"+hashlib.sha1(f"{c[\"application_secret\"]}+{c[\"consumer_key\"]}+GET+{url}++{ts}".encode()).hexdigest()
  req=urllib.request.Request(url,headers={"X-Ovh-Application":c["application_key"],"X-Ovh-Consumer":c["consumer_key"],"X-Ovh-Timestamp":ts,"X-Ovh-Signature":sig})
  print(json.dumps(json.loads(urllib.request.urlopen(req).read()),indent=2))'
  ```
  Expect: `state=ok`, `bootId=218949` (rescue12-customer), `os=none_64`.
- Datacenter check: server's `datacenter` must be `hil1` (Hillsboro, OR). Other
  OVH HIL labels work, but `vin1`/`bhs5` are different regions and break the
  hardcoded `topology.kubernetes.io/region: hil` label.
- OVH API credentials at `secrets/ovh-credentials.sops.yaml` (see file header for
  required scopes).
- direnv loaded in `cluster/` so `PG_CONN_STR`, `TALOSCONFIG`, `KUBECONFIG`,
  `SOPS_AGE_KEY` are set.

## 1. Choose a slot

All five slot variables below are currently occupied by provisioned servers (there is
no spare slot). To provision a genuinely new node, either add a new variable following
this naming pattern, or free up one of these slots via §5 (replacing an existing slot).

| Variable                        | Current hostname | Hardware |
| -------------------------------- | ----------------- | -------- |
| `kimsufi_service_name`           | `ovh-ns103656`    | KS-5     |
| `kimsufi_service_name_1`         | `ovh-ns103711`    | KS-5     |
| `kimsufi_service_name_cp0`       | `ovh-ns102453`    | KS-5     |
| `kimsufi_service_name_ks_game_0` | `ovh-ns104952`    | KS-GAME  |
| `kimsufi_service_name_ks_game_1` | `ovh-ns104963`    | KS-GAME  |

Slot ↔ Nebula identity is fixed in code (`cluster/terraform/main/ovh-nodes.tf`,
`local.kimsufi_servers`):

| Variable                          | Hostname       | Nebula IP       | Talos role    | Install disk                     | Data disk selector               |
| ---------------------------------- | -------------- | --------------- | ------------- | --------------------------------- | --------------------------------- |
| `kimsufi_service_name`             | `ovh-ns103656` | `10.42.0.13/16` | control plane | `/dev/sda`                       | `/dev/sdb`                       |
| `kimsufi_service_name_1`           | `ovh-ns103711` | `10.42.0.14/16` | worker        | `/dev/sda`                       | `/dev/sdb`                       |
| `kimsufi_service_name_cp0`         | `ovh-ns102453` | `10.42.0.15/16` | worker        | `/dev/sda`                       | `/dev/sdb`                       |
| `kimsufi_service_name_ks_game_0`   | `ovh-ns104952` | `10.42.0.16/16` | control plane | NVMe serial `BTPF8256006P450RGN` | NVMe serial `BTPF8304019P450RGN` |
| `kimsufi_service_name_ks_game_1`   | `ovh-ns104963` | `10.42.0.17/16` | control plane | NVMe serial `BTPF8256002V450RGN` | NVMe serial `BTPF8256009U450RGN` |

`data_disk_match` becomes a Talos `UserVolumeConfig` disk selector; it mounts at
`/var/mnt/seaweedfs-data` (legacy name) or `/var/mnt/local-path-ovh-<tier>` on nodes
that have gone through the OVH storage-tiering rename (see `ovh-nodes.tf`'s
`data_disk_mount_renamed_nodes`) — `local-path-ovh` uses whichever path applies on
each listed node.

## 2. Set the service name

Update the default in `cluster/terraform/main/variables.tf` to the new server's
OVH service name (e.g. `ns103711.ip-147-135-39.us`). Commit so the value is
reproducible across machines.

(Alternative: `export TF_VAR_kimsufi_service_name=...` for a one-off run.)

## 3. Targeted `tofu apply`

```bash
cd cluster/terraform/main
tofu apply \
  -target='ovh_dedicated_server.kimsufi' \
  -target='ovh_dedicated_server_update.kimsufi_rescue' \
  -target='ovh_dedicated_server_reboot_task.kimsufi_to_rescue' \
  -target='null_resource.install_talos_kimsufi' \
  -target='ovh_dedicated_server_update.kimsufi_harddisk' \
  -target='ovh_dedicated_server_reboot_task.kimsufi_to_talos' \
  -target='talos_machine_configuration_apply.kimsufi' \
  -target='null_resource.nebula_node_cert'
```

The OVH chain runs only for the slot whose `service_name` changed from `""` to
a real value. Roughly 8–12 minutes:

1. `PUT /dedicated/server/{name}` — rescue SSH key, EFI bootloader path
2. `PUT .../update` — set bootId=218949 (rescue), then `POST .../reboot`
3. SSH into rescue, `dd` Talos metal image to the slot's `install_disk`
4. `PUT .../update` — set bootId=1 (harddisk), then `POST .../reboot`
5. `PUT /machine/config` over Talos API — joins cluster, configures Nebula

Targeted apply avoids the slow full-root refresh (Proxmox provider stalls on
offline `atlas`).

If `atlas`/Proxmox is offline during a control-plane migration, use targeted
plans for OVH only and leave Proxmox-managed resources in state. Once
Proxmox is reachable again, run a reviewed full plan from `cluster/terraform/main`
to converge the now-empty `local.proxmox_nodes` map, destroy
`proxmox_virtual_environment_vm.talos["pve_cp0"]`, and prune the retired local
Nebula cert null-resources.

## 4. Verify

```bash
kubectl get nodes -l topology.kubernetes.io/zone=hil-ovh -w
# Wait for the new node to reach Ready (~2 min after machine config applies).

# Nebula mesh check (from any other node):
nebula-cert print -ca /path/to/ca.crt -path nebula.crt  # cert is valid
ping 10.42.0.13  # or .14, depending on slot
```

If the new node shares a public `/24` with an existing Kimsufi node, also verify
public Talos reachability between those nodes. Nebula and kubelet may look healthy
while public peer traffic still fails.

```bash
# Direct API readiness for each Kimsufi control-plane public IP
# (ovh-ns103656, ovh-ns104952, ovh-ns104963 — see nebula-mesh.json for the roster).
kubectl --server=https://147.135.39.162:6443 --insecure-skip-tls-verify=true get --raw='/readyz?verbose'
kubectl --server=https://147.135.104.5:6443 --insecure-skip-tls-verify=true get --raw='/readyz?verbose'
kubectl --server=https://147.135.104.16:6443 --insecure-skip-tls-verify=true get --raw='/readyz?verbose'

# Cross-node Talos API/maintenance reachability from host-networked Kimsufi pods.
# Use one Cilium pod per Kimsufi node as the source and test the other public IPs.
kubectl -n kube-system exec <cilium-pod-on-source-node> -- \
  bash -lc 'for ip in 147.135.37.175 147.135.39.162 147.135.39.176 147.135.104.5 147.135.104.16; do timeout 2 bash -lc "</dev/tcp/${ip}/50000" && echo "${ip} ok" || echo "${ip} fail"; done'
```

Add the new public IP to `cluster/nebula-mesh.json` so non-Talos nodes (wyrm2,
rugged) have a direct path instead of relying on stale relay paths. Commit.

## 5. Replacing an existing slot

OVH cancellation only stops billing renewal — the old server keeps running until
expiry. Two strategies:

**A. Drain-then-replace** (clean K8s identity, brief disruption):

1. `kubectl cordon <hostname>` (e.g. `ovh-ns103711` — the Talos `HostnameConfig` sets
   the k8s Node name to the slot's hostname, not a `talos-kimsufi-*` label)
2. `kubectl drain --ignore-daemonsets --delete-emptydir-data <hostname>`
3. `talosctl -n <old-ip> shutdown` (so old kubelet stops claiming the hostname)
4. `kubectl delete node <hostname>`
5. `tofu state rm` the 9 entries for that slot (`data.ovh_dedicated_server`,
   `ovh_dedicated_server`, `..._update.kimsufi_{rescue,harddisk}`,
   `..._reboot_task.kimsufi_to_{rescue,talos}`, `null_resource.install_talos_kimsufi`,
   `data.talos_machine_configuration`, `talos_machine_configuration_apply` — all
   under `["<hostname>"]`, since `local.kimsufi_servers` is keyed by hostname).
6. Continue from §2.

**B. Use the empty slot first, drain later** (additive, zero disruption):

1. Provision the new server into the unused slot (§2–§4).
2. Once it's joined, cluster has surplus capacity.
3. Then do (A) for the slot you're retiring.

Use B when the slot to retire is hosting load-bearing pods that can't easily
relocate (worker memory pressure is the usual reason).

## Gotchas

- **Rescue bootId is hardcoded to 218949** (`rescue12-customer`, Debian 12).
  `data.ovh_dedicated_server_boots` returns both rescue and `ipxe-shell` with
  no kernel name to filter by; picking [0] silently selects iPXE shell and SSH
  never comes up.
- **`efi_bootloader_path = "\efi\boot\bootx64.efi"`** is mandatory — without it
  OVH chains rEFInd, which "launches" the Talos UKI but never returns control,
  causing a silent boot loop.
- **`console=ttyS0,115200n8`** in `extraKernelArgs` — KS-5 has no display; this
  is the only way to see boot via OVH IPMI SOL.
- **Region hardcoded to `hil` / `hil-ovh`** in each Kimsufi slot definition.
  If the server lands in a non-HIL datacenter (e.g. `vin1`), fix the
  slot's `zone` before applying or you'll mis-label the node.
- **`ovh_dedicated_server_update` is deprecated** but still used as an
  imperative boot-mode step for rescue -> install -> harddisk. The provider
  replacement is `ovh_dedicated_server`, which can express only one desired
  `boot_id` at a time for the canonical server resource. Replace this with a
  small explicit OVH API helper before moving to OVH provider v3.
- **`ovh_dedicated_server` auto-syncs `iam.displayName`** into state, which
  requires `PUT /services/*` scope. The HCL keeps `display_name = each.value.service_name`
  so state matches config and the PUT is never attempted — leave this in place
  while any cancelled server is in state, because PUT against a cancelled
  service hangs ~10 min then times out. (See lessons-learned §2.)
- **When replacing a slot, `tofu apply` will try to update the cancelled
  predecessor in-place** (provider behavior). Either restrict `-target=` to the
  new slot only (`...["kimsufi_workerN"]`) or `tofu state rm` the cancelled
  slot's 9 entries before applying.
- **Tofu plan is slow** on the full root if Proxmox `atlas` is offline (provider
  hangs on network timeouts). Always use `-target=` for ad-hoc operations.
- **Kimsufi peers in the same public `/24` still need explicit host routes.**
  OVH assigns addresses that look same-subnet, but traffic between those public
  peers must go through the per-subnet gateway (`<first-three-octets>.254`) rather
  than direct neighbor resolution. `cluster/terraform/main/ovh-nodes.tf` computes
  `/32` peer routes in `local.kimsufi_eno1_peer_routes` and applies them with a
  Talos `LinkConfig` on `eno1`. Keep the paired `DHCPv4Config`; a route-only
  `LinkConfig` disables Talos' default DHCP operators. Roll route changes out one
  machine at a time, prefer `talosctl --mode=try` for the canary, and run the
  public `:50000` peer matrix before applying the next node.

## References

- Resource definitions: `cluster/terraform/main/ovh-nodes.tf`,
  `cluster/terraform/main/nebula.tf`, `cluster/terraform/main/persistent-auth.tf`
- Service-name vars: `cluster/terraform/main/variables.tf`
- Mesh roster (where to register the new host): <mesh_membership.md>
- Historical gotchas: <lessons_learned/2026_05_13_provisioning_ovh_kimsufi.md>
