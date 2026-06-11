# Provisioning an OVH Kimsufi worker

Add a Kimsufi bare-metal worker (KS-5 in HIL) to the cluster. Talos is installed
via OVH rescue boot → `dd` of the metal image; then we apply a Talos machine
config that joins the cluster and brings up the Nebula extension.

The Terraform code in `cluster/terraform/main/ovh-nodes.tf` already declares two
slots (`kimsufi_worker0`, `kimsufi_worker1`) keyed by `for_each` over
`local.active_kimsufi_servers`. A slot is "active" iff its `service_name` variable
is non-empty.

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

| Scenario                                   | Slot                                           |
| ------------------------------------------ | ---------------------------------------------- |
| First Kimsufi worker                       | `kimsufi_worker0` (`var.kimsufi_service_name`) |
| Adding capacity alongside a healthy worker | empty slot (`worker_0` or `worker_1`)          |
| Replacing an existing slot                 | the slot to replace — see §5                   |

Slot ↔ Nebula identity is fixed in code:

| Slot              | Hostname                 | Nebula IP       | TF var                   |
| ----------------- | ------------------------ | --------------- | ------------------------ |
| `kimsufi_worker0` | `talos-kimsufi-worker-0` | `10.42.0.13/16` | `kimsufi_service_name`   |
| `kimsufi_worker1` | `talos-kimsufi-worker-1` | `10.42.0.14/16` | `kimsufi_service_name_1` |

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
3. SSH into rescue, `dd` Talos metal image to `/dev/sda`
4. `PUT .../update` — set bootId=1 (harddisk), then `POST .../reboot`
5. `PUT /machine/config` over Talos API — joins cluster, configures Nebula

Targeted apply avoids the slow full-root refresh (Proxmox provider stalls on
offline `atlas`).

## 4. Verify

```bash
kubectl get nodes -l topology.kubernetes.io/zone=hil-ovh -w
# Wait for the new node to reach Ready (~2 min after machine config applies).

# Nebula mesh check (from any other node):
nebula-cert print -ca /path/to/ca.crt -path nebula.crt  # cert is valid
ping 10.42.0.13  # or .14, depending on slot
```

Add the new public IP to `cluster/nebula-mesh.json` so non-Talos nodes (wyrm2,
rugged) have a direct path instead of relying on VPS relays. Commit.

## 5. Replacing an existing slot

OVH cancellation only stops billing renewal — the old server keeps running until
expiry. Two strategies:

**A. Drain-then-replace** (clean K8s identity, brief disruption):

1. `kubectl cordon talos-kimsufi-worker-N`
2. `kubectl drain --ignore-daemonsets --delete-emptydir-data talos-kimsufi-worker-N`
3. `talosctl -n <old-ip> shutdown` (so old kubelet stops claiming the hostname)
4. `kubectl delete node talos-kimsufi-worker-N`
5. `tofu state rm` the 9 entries for that slot (`data.ovh_dedicated_server`,
   `ovh_dedicated_server`, `..._update.kimsufi_{rescue,harddisk}`,
   `..._reboot_task.kimsufi_to_{rescue,talos}`, `null_resource.install_talos_kimsufi`,
   `data.talos_machine_configuration`, `talos_machine_configuration_apply` — all
   under `["kimsufi_workerN"]`).
6. Continue from §2.

**B. Use the empty slot first, drain later** (additive, zero disruption):

1. Provision the new server into the unused slot (§2–§4).
2. Once it's joined, cluster has surplus capacity.
3. Then do (A) for the slot you're retiring.

Use B when the slot to retire is hosting load-bearing pods that can't easily
relocate (VPS workers are tight on memory).

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
- **Region hardcoded to `hil` / `hil-ovh`** in `kimsufi_machine_config_patch`.
  If the server lands in a non-HIL datacenter (e.g. `vin1`), fix the
  `nodeLabels` block before applying or you'll mis-label the node.
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

## References

- Resource definitions: `cluster/terraform/main/ovh-nodes.tf`,
  `cluster/terraform/main/nebula.tf`, `cluster/terraform/main/persistent-auth.tf`
- Service-name vars: `cluster/terraform/main/variables.tf`
- Historical gotchas: <lessons_learned/2026_05_13_provisioning_ovh_kimsufi.md>
