# Atlas VM memory and virtiofs safeguards

The VM declarations live in
[`proxmox-vms.tf`](../cluster/terraform/main/proxmox-vms.tf), guest mounts in
[Wyrm2's configuration](../nix/nixos/hosts/wyrm2/default.nix), and host memory
limits in [`ansible/atlas.yaml`](../ansible/atlas.yaml). Read those declarations
for current sizes and policies.

## Keep the virtiofs cache safeguard

The Wyrm VM 100 investigation observed roughly 450,000 open FDs and tens of GiB
of shared-memory growth under directory-scanning workloads with `cache=auto`
or `cache=metadata`. The safeguard was lost when Wyrm2 replaced Wyrm; on
2026-03-18, Wyrm2's code-share worker reached 6.6 GiB RSS, leaving the host near
memory exhaustion. `cache=never` stopped the growth in both investigations.
The [chipset/memory investigation](../debug/atlas/black_screen_lockup.md)
retains the incident evidence.

Keep `cache = "never"` on both `tankshare` and `code`. `metadata` is not equivalent:
virtiofsd 1.13.3's directory/dentry caching could still retain FDs even with direct
file I/O. A new VM or mount must carry the safeguard too.

On Atlas, inspect each virtiofsd process's RSS and FD count and compare the running
VM configuration with the declaration:

```bash
pgrep -a virtiofsd
ps -p <pid> -o rss=
ls /proc/<pid>/fd | wc -l
qm config <vmid>
```

Past healthy mounts had tens of FDs; the old note used 10,000 as an investigation
trigger, not a service limit. `qm` cache-policy changes need a VM restart.
Do not treat a declaration edit as proof that existing workers were restarted.

## Host memory pressure

VM allocations, virtiofsd memory, ZFS ARC and host processes all compete for host
RAM. The original overcommit reached 136 GiB of VM allocations plus 12 GiB ARC
and host overhead on a 128 GiB machine. The playbook now declares the ARC cap;
check actual usage and `/sys/module/zfs/parameters/zfs_arc_max` when pressure
recurs rather than copying a historical memory budget.

## Shared writable mappings

The investigated `cache=never` virtiofs path rejected `MAP_SHARED` mappings used
by devenv/Nix and libgit2 tooling with `ENODEV`. Changing to another FUSE-based
transport was not shown to solve it. A future transport change must test the
actual writable-mapping workload and host-memory behavior; the old observation
is not a claim about every FUSE client or future virtiofsd version.
