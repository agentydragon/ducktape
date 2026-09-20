# Runbook: serial Talos cluster upgrades

This runbook upgrades the installed Talos OS on one Kubernetes node at a time.
It covers the OVH bare-metal fleet and the home OptiPlex worker. It is not a
Kubernetes-version upgrade, a node reprovision, or a disk/PVC migration.

The Terraform machine-configuration resource only reconciles the configured
installer image; it does **not** upgrade the running OS. After applying that
configuration to one node, the operator must run `talosctl upgrade` for that
same node. Keep the explicit hostnames and network configuration intact:
<../lessons_learned/2026_03_07_talosctl_upgrade_hostname_loss.md> documents
how hostname/CIDR drift plus an unsafe targeted apply previously caused server
replacement and loss of etcd quorum.

## Safety rules

- Upgrade one node at a time. Finish the post-upgrade checks and restore the
  node to service before starting another node.
- Upgrade workers before control planes. Upgrade control planes one at a time,
  and only while all three etcd members are healthy. Never pass `--force` to
  `talosctl upgrade`.
- Cordon and drain with Kubernetes eviction, without `--force` or
  `--disable-eviction`. A PDB rejection is a stop-and-investigate signal, not
  something to bypass.
- Never delete a PVC or PV as part of a Talos OS upgrade. Local-path data stays
  on its original node; a pod using it may remain Pending until that node is
  back and uncordoned. EmptyDir contents are ephemeral and are discarded by
  `--delete-emptydir-data`.
- Before rolling a node with a SeaweedFS volume server, require a clean
  `volume.fix.replication -n` dry-run. Repeat the check after every volume
  server roll. Do not roll any volume host while repair is still running or
  under-replicated volumes remain.
- If a critical single-replica workload has no approved downtime, or safe
  eviction cannot proceed without force, stop and ask the operator. A
  disposable testing database may be unavailable only when that downtime has
  been explicitly approved; preserve and reuse its existing PVC.
- Keep the Proxmox Talos pin separate. `proxmox_talos_version` is intentionally
  held back for the documented AMD/KVM kernel issue; do not change it as part
  of this fleet upgrade without a separate review.

## 1. Establish the baseline

Use the repository devshell and the cluster's operator-authorized kubeconfig.
Confirm the target node's role, Talos/Kubernetes versions, internal IP,
hostname, labels, taints, podCIDR, workloads, PVCs, PDBs, and local PV node
affinity. Record the live mapping before making changes:

```bash
kubectl get nodes -o wide
kubectl get pods -A -o wide
kubectl get pdb -A -o wide
kubectl get pv -o wide
kubectl top nodes
```

Review CNPG state with `kubectl cnpg -n <namespace> status <cluster>` (or list
`Cluster` resources and inspect their status). Every multi-instance cluster
that could be affected must have all instances healthy and a ready replica on
another node. Identify the target's current primary before draining.

Check Flux, active alerts, and recent warning events. Record known unrelated
baseline conditions (for example, intentionally offline roaming nodes) and
do not count them as new roll failures. Do not start during an unexplained
control-plane, storage, replication, or application incident.

For any OVH node hosting a `seaweedfs-volume-*` pod, check the current repair
jobs/logs and run this dry-run against a healthy filer:

```bash
kubectl exec -i -n seaweedfs seaweedfs-filer-0 -- weed shell <<'EOF'
volume.fix.replication -n
EOF
```

Proceed only when the dry-run reports no under-replicated volumes and no
repair job is still copying data. The cluster may have an active repair job
even when all pods are Ready.

## 2. Prepare only the target's Terraform machine configuration

`talos_version` is the version used by the provider to generate machine
configuration; it is not the running OS upgrade target. Keep it at the current
configuration contract during a serial OS roll, and set the selected node's
desired OS release in `talos_installer_version_overrides`. This selects the
target `machine.install.image`; `talosctl upgrade` performs the actual OS
upgrade. The target plan can still contain preexisting machine-configuration
drift, so apply only if the complete config diff passes the image-only gate.
Do not bump the shared configuration-generator version as part of a node roll:
it can also change generated extension configuration (for example, Nebula peer
data), which must be reviewed separately. Keep unrelated pins (including
`proxmox_talos_version`) unchanged. Keep `talos_machine_secrets_version`
pinned to the version contract that generated the durable cluster secrets; an
OS upgrade does not require rotating those secrets.

For an OVH node, the target is
`talos_machine_configuration_apply.kimsufi["<node>"]`; this map includes both
workers and control planes, with role taken from the node roster. For OptiPlex,
the target is `talos_machine_configuration_apply.home_worker["optiplex"]`.

From the repository root, create and inspect a _saved, targeted_ plan for just
the intended node:

```bash
bb run @multitool//tools/tofu:tofu -- \
  -chdir=cluster/terraform/main plan \
  -target='talos_machine_configuration_apply.kimsufi["<node>"]' \
  -out=/tmp/talos-<node>.tfplan
bb run @multitool//tools/tofu:tofu -- \
  -chdir=cluster/terraform/main show -no-color /tmp/talos-<node>.tfplan
```

Substitute the `home_worker` address for OptiPlex. Read the complete plan,
including the machine-configuration payload. It must update only the selected
node's Talos machine configuration in place, and the only changed
machine-configuration path must be `machine.install.image` at the expected
version. Reject any other changed path (even if its drift is understood),
add, delete, replacement, OVH server/boot action, disk/schema change, unrelated
node update, or unexplained drift. Do not run the full cluster bootstrap/apply.

If the target's plan contains other machine-configuration drift, do not apply
it. An explicit `talosctl upgrade --image ...` (step 4) changes the installed
OS without reconciling that configuration; it may be used only when that
version-only operation is approved and the unapplied configuration drift is
recorded for separate review. Do not silently bundle the drift into an OS
upgrade or claim that Terraform configuration has been reconciled.

When the plan passes the image-only gate, apply only the exact saved plan after
review:

```bash
bb run @multitool//tools/tofu:tofu -- \
  -chdir=cluster/terraform/main apply /tmp/talos-<node>.tfplan
```

The saved plan is the application boundary; do not regenerate it between
review and apply. If it is stale, discard it and review a new plan.

Before cordoning the node, verify that the exact installer image from the plan
is resolvable:

```bash
docker manifest inspect \
  factory.talos.dev/metal-installer/<fleet-schematic-id>:<talos-version>
```

Do not infer registry availability from Terraform state alone: the
`talos_image_factory_schematic` provider resource does not refresh its remote
schematic on read. If the manifest lookup fails, restore/verify the existing
schematic and retry the lookup before beginning node downtime.

A successful schematic-registration response is not sufficient if the
follow-up schematic or installer lookup still returns `404`. Stop before
cordoning the node and escalate that inconsistency to Image Factory; do not
repeat Terraform applies as a way to trigger an image build. Image Factory
builds installer images on demand when they are pulled, as described in the
[Image Factory API documentation](https://github.com/siderolabs/image-factory/blob/main/docs/api.md).

## 3. Evacuate the target without force

### CNPG primary on the target

For each healthy multi-instance CNPG cluster whose primary is on the target,
choose a healthy replica on another node and request a planned switchover:

```bash
kubectl cnpg -n <namespace> status <cluster>
kubectl cnpg -n <namespace> promote <cluster> <ready-replica-instance>
kubectl cnpg -n <namespace> status <cluster>
```

Confirm the new primary is the selected off-node replica and every instance is
healthy before continuing. This uses CNPG's planned promotion path; do not
delete the primary pod or its PVC to force a role change.

### Explicitly approved single-instance testing CNPG

A single-instance cluster has no replica to promote and its PDB correctly
blocks drain by default. Only for a disposable/test cluster whose downtime has
been explicitly approved, use CNPG's node-maintenance window with PVC reuse.
For the currently approved `agentplane-testing/postgres` exception:

```bash
kubectl cnpg -n agentplane-testing maintenance set postgres --reusePVC -y
kubectl get cluster -n agentplane-testing postgres -o yaml
kubectl get pdb -n agentplane-testing
```

Verify `nodeMaintenanceWindow.inProgress: true`, `reusePVC: true`, and that the
operator has removed the blocking PDB. This allows the instance to stop and
reuse its existing node-local PVC after the node returns. Never delete that
PVC. For any other single-instance or critical database, stop for explicit
approval rather than disabling its PDB.

Then cordon and drain:

```bash
kubectl cordon <node>
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data --timeout=15m
```

Do not add `--force`, `--disable-eviction`, or `--delete-local-data`. If drain
is blocked, inspect the named pod/PDB and resolve it with its owning operator
(for CNPG, perform the planned switchover or approved maintenance procedure).
If safe eviction is still impossible, stop and ask. Do not proceed with the
Talos upgrade while an unexplained workload remains on the node.

## 4. Upgrade exactly one Talos node

Use the fleet's versioned Image Factory installer URL, including the active
schematic ID and target Talos release. Confirm both from the reviewed
Terraform plan/state; do not substitute a generic installer that omits the
cluster's system extensions. Address the node directly for both endpoint and
target so the request cannot fan out to other members:

```bash
talosctl \
  --endpoints <node-nebula-ip> \
  --nodes <node-nebula-ip> \
  upgrade \
  --image factory.talos.dev/metal-installer/<fleet-schematic-id>:<talos-version> \
  --drain=false \
  --wait
```

The node was already cordoned and drained explicitly. Do not upgrade multiple
nodes in one command. If the client loses its stream during reboot, verify
state directly rather than starting a second upgrade: inspect Talos version,
boot/machined health, and the Kubernetes Node object.

## 5. Verify and return the node to service

Before uncordoning, verify all of the following:

1. `kubectl get node <node> -o wide` is `Ready`, with the same node name,
   InternalIP, podCIDR, expected labels, and role/taints as before.
2. `talosctl version` addressed directly to the node reports the target
   version; Talos services are healthy. For a control plane, `talosctl etcd
members` still shows all expected members and `talosctl etcd status` shows
   a healthy member/quorum.
3. Cilium and other required node DaemonSets are Ready. No PVC/PV was deleted
   or recreated. Workloads using node-local PVs may remain Pending while the
   node is cordoned; they cannot be rescheduled until it is uncordoned.
4. If this is a control plane, verify etcd quorum is healthy before continuing.

Then return the recovered node to scheduling:

```bash
kubectl uncordon <node>
kubectl get node <node>
```

Wait for local-PV pods to rebind to their original PVCs and for the node's
workloads to return. Then verify:

1. CNPG clusters are healthy with the expected primary and all expected
   replicas. Restore any temporary testing-cluster maintenance window only
   after its instance is Running/Ready on the preserved PVC:

   ```bash
   kubectl cnpg -n agentplane-testing status postgres
   kubectl cnpg -n agentplane-testing maintenance unset postgres -y
   ```

2. If this node hosted a SeaweedFS volume server, it is Ready and the
   replication dry-run is clean again.
3. Flux and application health have no new unexplained failures. Compare
   active alerts and warning events with the pre-roll baseline.

Wait for workloads and replicas to settle before choosing the next node. A
second control-plane upgrade must not start until the first member is fully
healthy and etcd quorum is restored. If hostname/IP/podCIDR changes, etcd is
degraded, a PVC is missing, Talos rolls back, or workloads fail to recover,
stop; do not continue the fleet or run a broad Terraform apply. Diagnose the
node and preserve the evidence first.

## 6. Fleet completion

After the final node, verify every active Talos node reports the intended
version and Ready state, all three etcd members are healthy, CNPG and SeaweedFS
replication are healthy, all local-PV workloads have returned, and the
cluster-wide baseline is no worse than before. Keep a per-node record of the
Terraform plan reviewed/applied, upgrade result, health checks, and any
approved maintenance exception.
