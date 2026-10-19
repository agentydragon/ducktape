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
the mount's other consumers. This is an experiment stop threshold, not a reserved
or enforced pool quota. Measure free space and allocated bytes throughout; stop
before the budget or the mount's existing operational headroom is exhausted.
Use small volumes for fill/resize tests; a separate 50GiB logical volume tests
sparse creation without filling it. Both initial pools use HDDs, so their
performance does not establish SSD performance on other hosts.

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

After both hosts pass preparation checks, a separate cdk8s/Flux change must pin
the operator/CSI components and configure:

- Satellites selected by the canary label, with the Talos-specific module-loader
  overrides. Both initial nodes use extension-provided modules.
- Explicit advertised replication addresses, ports and network access over the
  mesh; validate satellite network-namespace lifetime and restart behavior.
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
[Talos boot assets](https://docs.siderolabs.com/talos/v1.14/platform-specific-installations/boot-assets).
