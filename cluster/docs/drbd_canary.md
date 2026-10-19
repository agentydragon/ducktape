# DRBD canary host preparation

Prepare the Talos workers `ovh-ns102453` (Nebula `10.42.0.15`) and
`ovh-ns103656` (Nebula `10.42.0.13`) for a disposable Piraeus/LINSTOR evaluation.
This change supplies kernel-module configuration and the `storage.allegedly.works/drbd-canary=true`
eligibility label. The label does not certify readiness. No Piraeus operator,
satellite, StorageClass, storage pool, or PVC is installed by this change.

The requirements and provider comparison are in
[research PR #11009](https://github.com/agentydragon/ducktape/pull/11009).
Host preparation does not establish that thin raw-block provisioning or same-PVC
relocation works; those require the disposable-volume acceptance below.

`wyrm2` remains outside the first canary to avoid disrupting the workstation
running this investigation. NixOS integration and cross-OS relocation remain
required before adopting a class for all hosts.

## Artifacts and storage placement

Both Talos canary workers stay on **v1.14.0**. Their Image Factory schematic preserves the
existing Nebula extension and console arguments and adds `siderolabs/drbd`:

```text
factory.talos.dev/metal-installer/05bbb2b04f1ad943be4a916bc2687d1e82c48a538ed441e35a980df8c1b800a3:v1.14.0
```

The installer OCI index was retrieved successfully on 2026-10-18 with digest
`sha256:2457d78b696346a5324ba2aeb9f38788a21f290312797535e275449494ddd204`;
it includes a Linux/amd64 manifest for both workers. This verifies registry metadata
availability, not a successful node upgrade.

On 2026-10-18, the official v1.14.0 catalog resolves DRBD to
`ghcr.io/siderolabs/drbd:9.3.3-v1.14.0`, digest
`sha256:fe3969622e20e41a470b4a7af2f3ac09573977b61e745117be023f661dc40f04`.
Recheck the exact installer manifest before beginning node downtime. If the base
schematic changes, obtain the new canary image from the Terraform output and
review its complete extension/kernel-argument set again.

The proposed `FILE_THIN` pools use these separate directories on existing mounts:

| Node           | Existing backing mount                           | Future canary pool directory                 |
| -------------- | ------------------------------------------------ | -------------------------------------------- |
| `ovh-ns102453` | `/var/mnt/local-path-ovh-hdd`, XFS on `/dev/sdb` | `/var/mnt/local-path-ovh-hdd/linstor-canary` |
| `ovh-ns103656` | `/var/mnt/local-path-ovh-hdd`, XFS on `/dev/sdb` | `/var/mnt/local-path-ovh-hdd/linstor-canary` |

Do not rename Talos UserVolumes, format disks, or change the existing local-path
or OpenEBS pools. Before creating either directory, verify that its parent is the
expected mounted filesystem; never let a missing mount silently redirect storage
onto the system disk. Pool creation belongs to the subsequent Piraeus deployment.

For the first tests, budget **20GiB physical allocation per node**, shared with
the mount's other consumers. A `FILE_THIN` pool reports the backing filesystem's
capacity to LINSTOR; the pool directory is not a quota, and LINSTOR can provision
logical volume sizes against the filesystem's available capacity. The 20GiB
budget is an external experiment stop threshold, not reserved or enforced space.
Sparse files consume physical space as they are written, alongside local-path and
OpenEBS data on the same filesystem. Measure LINSTOR pool capacity, filesystem
free space, and canary directory allocation throughout; stop before the budget
or the mount's existing operational headroom is exhausted. Use small volumes
for fill/resize tests; a separate 50GiB logical volume tests sparse creation
without filling it. Both initial pools use HDDs, so their performance does not
establish SSD performance on other hosts.

## Talos rollout

Perform this during an approved maintenance window. Use the repository devshell,
an operator-authorized kubeconfig/Talos config, and the baseline, workload,
CNPG/SeaweedFS availability, drain, and post-upgrade gates in the
[serial upgrade runbook](runbooks/talos_cluster_upgrades.md).
Review the target's actual workloads and local PVs before accepting downtime.

**Keep bootstrap and upgrade separate.** `talos_image_factory_schematic.kimsufi`
also feeds the OVH rescue/dd install triggers. Leave it and those resources
unchanged. The new `talos_image_factory_schematic.drbd_canary` is used only by
the two canary workers' machine-configuration patches and explicit in-place upgrades.
Do not run a blanket cluster bootstrap/apply for this rollout.

Register the schematic once. Then complete the node-specific steps below for
`ovh-ns102453` first; restore normal scheduling and verify workload recovery
before starting `ovh-ns103656`. For the second worker substitute its hostname
and `10.42.0.13` in every target and command. Never drain or reboot both together.

1. Create and review a saved plan targeting only
   `talos_image_factory_schematic.drbd_canary`, using the Bazel-managed ToFu
   command from the serial upgrade runbook. It may only register that new
   schematic (or be a no-op if already recorded). Apply that exact reviewed
   plan. Registration in Image Factory is idempotent; it does not install the
   image on any node.
2. Verify the exact installer manifest is retrievable. Record the current OS,
   installed extensions, machine configuration, data-volume/mount identities,
   workload health, and old installer image for recovery. Preserve a private
   configuration copy; machine configurations contain credentials.
3. Cordon and drain **only** `ovh-ns102453`, following the existing runbook.
   Resolve PDBs and workload-specific availability requirements before eviction.
4. Upgrade using the new image while preserving the current machine configuration:

   ```bash
   talosctl --endpoints 10.42.0.15 --nodes 10.42.0.15 upgrade \
     --image factory.talos.dev/metal-installer/05bbb2b04f1ad943be4a916bc2687d1e82c48a538ed441e35a980df8c1b800a3:v1.14.0
   ```

   This is an in-place OS-image upgrade and reboot. Do not apply the DRBD-loading
   machine patch before the extension is installed: the old image lacks the module.

5. After the node returns and the extension is present, create a new saved plan
   targeting only `talos_machine_configuration_apply.kimsufi["ovh-ns102453"]`.
   Review the complete machine-configuration diff. The permitted changes are
   the canary installer image, the `drbd`/`drbd_transport_tcp` module entries
   (`usermode_helper=disabled` on DRBD), and the canary label. The factory resource
   should already be recorded. Reject server/boot/install actions, disk changes,
   unrelated node updates, replacements, secrets changes, or other drift. Apply
   only the exact reviewed plan. The existing apply mode is
   `staged_if_needing_reboot`; inspect whether Talos staged anything, and if so
   complete a second controlled reboot before declaring preparation complete.
6. Verify modules, parameters, and unchanged storage before uncordoning:

   ```bash
   talosctl --endpoints 10.42.0.15 --nodes 10.42.0.15 get extensions
   talosctl --endpoints 10.42.0.15 --nodes 10.42.0.15 read /proc/modules
   talosctl --endpoints 10.42.0.15 --nodes 10.42.0.15 read /sys/module/drbd/version
   talosctl --endpoints 10.42.0.15 --nodes 10.42.0.15 read /sys/module/drbd/parameters/usermode_helper
   talosctl --endpoints 10.42.0.15 --nodes 10.42.0.15 read /proc/devices
   talosctl --endpoints 10.42.0.15 --nodes 10.42.0.15 get volumestatus
   talosctl --endpoints 10.42.0.15 --nodes 10.42.0.15 get mountstatus
   ```

   Expect DRBD9, `drbd_transport_tcp`, helper `disabled`, loop block-device
   support, and the original data mount. Loop support is built into the Talos
   kernel, so it need not appear in `/proc/modules`. Check node identity,
   networking, labels, taints, and workload recovery against the baseline.

## Later NixOS validation

The pinned Nixpkgs DRBD **9.2.16** module was built successfully for Linux
**6.18.55**, including the TCP transport; its version and vermagic were inspected.
The live check on 2026-10-18 found `wyrm2` running **6.18.54**, so that artifact cannot be loaded into
its running kernel. This demonstrates a build path, not working PVC provisioning
or compatibility between the NixOS and Talos DRBD versions.

There are no `wyrm2` configuration changes in this preparation. A later NixOS
canary should declare `config.boot.kernelPackages.drbd`, DRBD utilities and udev
rules, and the required modules while leaving `services.drbd` disabled so
LINSTOR owns resource creation. Plan that validation for a convenient maintenance
window or use a separate NixOS test node. DRBD does not inherently require a
NixOS reboot if a suitable module can be supplied for the running kernel, but
loading one on the active workstation is outside this first canary.

For a later `wyrm2` pool, the candidate remains
`/var/local-path-provisioner/linstor-canary` on the existing ext4 `/dev/vda`
filesystem. Unlike the initial XFS pools, that filesystem does not supply
reflinks needed for LINSTOR file-backed snapshots. Test NixOS-specific satellite
configuration, local-only acknowledgment, raw block I/O, and relocation to/from
Talos before claiming the universal class requirement is met.

## Piraeus deployment and acceptance follow-up

After both hosts pass preparation checks, reconcile the operator and LINSTOR
configuration declared in `cluster/cdk8s/piraeus.py`. Before provisioning any
PVC, verify:

- Satellites selected by the canary label, with the Talos-specific module-loader
  overrides. Both initial nodes use extension-provided modules.
- The LINSTOR chart enables `createInternalTLS: cert-manager` and
  `createApiTLS: cert-manager`. The cluster Flux unit depends on both the
  operator (for its CRDs) and cert-manager before applying those resources. The
  chart creates Issuers and Certificates in `piraeus-datastore`; cert-manager
  creates the private keys at runtime, so no keys are committed. Controller to
  Satellite traffic uses mutual TLS on port 3367, and CSI/controller API clients
  use HTTPS with client certificates on port 3371. The stock operator TLS patch
  removes plaintext port 3366 and retains its 3367 listener binding: its
  liveness probe connects to `localhost:3367` before checking certificate
  rotation. Since Satellites use host networking, that authenticated mTLS
  listener is bound on host interfaces, including public interfaces; this canary
  does not override the stock binding. The controller still has its plain HTTP
  API on port 3370 for health probes. A NetworkPolicy selecting only the
  operator's `app.kubernetes.io/component=linstor-controller` Pod label admits
  ordinary Pod ingress only on TCP 3371, where LINSTOR mTLS authenticates
  clients. Cilium's default local-host exemption lets kubelet probes reach 3370;
  host-local traffic remains outside this Pod policy. The controller itself does
  not use host networking.
- LINSTOR reports the two Nebula node addresses (`10.42.0.15` and `10.42.0.13`)
  for host-networked satellites. The controller's canary-only `TcpPortAutoRange`
  is `7900-7919`; verify those TCP ports are available and reachable over the
  mesh. Piraeus documents that host networking reconfigures DRBD peer addresses;
  switching back to the pod network requires resetting paths or rebooting all
  involved nodes.
- The DRBD Reactor Prometheus endpoint defaults to unauthenticated HTTP on
  wildcard port 9942. The canary patches it to `127.0.0.1:9942`; monitoring is
  disabled, so this endpoint is local-only and is not scraped. DRBD replication
  still advertises the Nebula node addresses above. No host firewall policy is
  added here.
- The two `FILE_THIN` directories above, with capacity monitoring and no pool
  autodiscovery or disk preparation.
- An opt-in `local-canary` StorageClass: `WaitForFirstConsumer`, one disk-backed
  copy, local access, expansion, and `Retain` reclaim policy during evaluation.
  The canary node restriction is temporary evaluation scope, not the final
  universal class's placement policy.
- Recoverable LINSTOR metadata and a tested controller-restart procedure.

Use disposable volumes to demonstrate Block and Filesystem provisioning,
physical allocation, logical capacity enforcement, expansion, discard, reboot
persistence, and explicit cleanup. Test existing mounted writes and `fsync`
with the other storage peer unreachable; distinguish these from provisioning
or new attachments that need the control plane.

Then move a volume both ways with online initial synchronization and a stopped
workload fallback. Record PVC UID, CSI volume handle, data integrity, physical
placement/copy count, acknowledgment policy, write latency, and cutover downtime.
Interrupt synchronization and controllers and prove source authority until
handoff. Repeat local-only acknowledgment tests after each completed move.
Asynchronous replication during the move is a candidate to test, not an
established migration guarantee.

## Recovery and removal

Before any canary volumes exist, recovery is restoring the previous reviewed
Talos machine configuration and installer image on the affected worker.
Talos upgrade/rollback and any extra reboot still require
the ordinary maintenance gates. Verify Nebula, storage mounts, and workloads
after recovery; do not reset or reinstall either node.

Once canary volumes exist, stop their workloads and explicitly migrate or
delete only the disposable canary data before removing satellites or modules.
Keep the LINSTOR metadata and backing files until ownership and cleanup are
verified. Never unload an in-use DRBD module or remove a pool directory as a
shortcut to teardown.

References: [Piraeus Talos integration](https://piraeus.io/docs/v2/how-to/talos/),
[Piraeus FILE_THIN pool configuration](https://piraeus.io/docs/v2/reference/linstorsatelliteconfiguration/),
[Piraeus internal TLS](https://piraeus.io/docs/v2/how-to/internal-tls/),
[Piraeus API TLS](https://piraeus.io/docs/v2/how-to/api-tls/),
[Piraeus host networking](https://piraeus.io/docs/v2/how-to/drbd-host-networking/),
[Cilium's local-host NetworkPolicy behavior](https://docs.cilium.io/en/stable/network/kubernetes/concepts/#default-ingress-allow-from-local-host),
[Talos boot assets](https://docs.siderolabs.com/talos/v1.14/platform-specific-installations/boot-assets).
