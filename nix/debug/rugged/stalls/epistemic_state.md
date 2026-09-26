# Rugged Xe/TTM Reclaim Investigation — Epistemic State

## Objective

Explain and eliminate the active swap/stall episodes on `rugged` without
mistaking historical zram occupancy for the cause. The operative question is
why the kernel pages aggressively while the Normal zone has many GiB free.

## Available action space

1. Capture another naturally occurring episode with page-type and Xe debugfs
   state.
2. Establish whether the booted `7.1.2` kernel contains the upstream Xe/TTM
   fragmentation-loop repair.
3. Upgrade/test a kernel carrying that repair, then compare the same counters.
4. If the repair is present or insufficient, file an upstream Xe report with
   the existing call graphs and the next triggered capture.

## Uncertainty register

| Question                                                       | What would resolve it                                                                                      |
| -------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Is the known upstream loop the complete local mechanism?       | A triggered capture showing `xe_shrinker` activity/BO eviction alongside rebinds, or an A/B kernel result. |
| What prevents a 2 MiB block from forming?                      | Before/after `/proc/pagetypeinfo` from a triggered capture.                                                |
| Is a particular application uniquely responsible for BO churn? | Per-client Xe DRM debugfs data during an episode.                                                          |
| Does the booted Nix kernel contain a backport?                 | Exact Nix kernel source/patch provenance, not just `uname -r`.                                             |

## Hypothesis space

| Hypothesis                                                                                       | Prior | Current posterior | Status                                                                                                         |
| ------------------------------------------------------------------------------------------------ | ----: | ----------------: | -------------------------------------------------------------------------------------------------------------- |
| The published Xe/TTM fragmentation reclaim/eviction/rebind loop is occurring.                    |   25% |               90% | Local trace now contains the same rebind/allocation path; an A/B kernel test remains the proof of remediation. |
| Xe/Iris has a separate BO leak or pinning regression which makes fragmentation unusually severe. |   35% |               20% | Plausible cofactor; not established.                                                                           |
| Chrome or GNOME has an app-specific rendering workload that is the primary trigger.              |   20% |                8% | Possible trigger, but both independent clients enter the same kernel path.                                     |
| Bazel/container/Kubernetes workload is the cause.                                                |   20% |                2% | Contradicted by the direct-compaction call graphs.                                                             |

## Evidence log

- With 16--19 GiB free and normal zone watermarks satisfied, `pswpout` rose by
  hundreds of MiB per ten seconds; this is current activity, not stale swap.
- The Normal zone had no free order-9/10 blocks (2/4 MiB), and compaction
  repeatedly failed.
- `/tmp/rugged-memory-fragmentation-20260713-191725` attributes direct
  compaction to Chrome's GPU process and GNOME Shell through Mesa Iris,
  `xe_ttm_tt_populate`, TTM, and the high-order page allocator.
- The upstream series _mm, drm/ttm, drm/xe: Avoid reclaim/eviction loops under
  fragmentation_ describes the same signature: substantial free RAM plus
  `kswapd -> shrinker -> eviction -> rebind (exec ioctl) -> repeat`; it names
  Chrome WebGL as a reproducer.
- The repair's Xe portion landed upstream as commit
  [`ba7fd1634228`](https://github.com/torvalds/linux/commit/ba7fd1634228)
  on 2026-06-11, after the v7.1 release. The v7.1.2 stable source lacks it.
  `rugged` boots NixOS `linux-7.1.2`, and the declared host configuration has
  no kernel patch/backport, so the booted kernel is treated as unpatched.
- The triggered capture `/tmp/rugged-memory-fragmentation-20260713-193606`
  hit 20,863 pages/s of swap-out and 76 compaction stalls/s before recording.
  In its 30 seconds, Chrome and GNOME Shell repeatedly ran
  `xe_exec_ioctl -> xe_vm_validate_rebind -> xe_ttm_tt_populate -> TTM` into
  high-order compaction while the Normal zone had no free order-9/10 blocks.
- Pinned `nixpkgs` and `nixpkgs-unstable` provide Linux 7.1.2. The repo's
  pinned Nixpkgs master exposes `linux_testing` 7.2-rc2, which contains the exact
  Xe beneficial-order change.

## Current posterior

This is no longer a generic fragmentation theory. It matches a known,
recently fixed-or-in-flight Xe/TTM pathological reclaim loop closely enough to
treat that loop as the leading root cause. The exact local failure is already
proven up to high-order Xe TTM allocation and VM reclaim. Whether the Xe
shrinker/eviction/rebind feedback leg is present, and which page types prevent
compaction, remain open measurements.

## Action queue

1. Run the triggered `capture_memory_fragmentation.sh` watcher from the report.
2. Inspect its `before/after-pagetypeinfo`, `gpu-debugfs`, and call graph.
3. Determine the kernel backport status and make an upgrade/test proposal.

## Decision tree

```text
triggered capture
  ├─ Xe shrink/rebind evidence + no 2 MiB blocks → test kernel with upstream fix
  ├─ fix present but loop persists              → upstream Xe report with capture
  └─ no Xe loop                                 → use page types/client data to test
                                                  a pinning/leak or app-trigger theory
```

## Stopping criteria

The investigation is complete only after an A/B result shows that a kernel
containing the upstream repair stops the swap storm, or after an upstream-ready
capture proves a distinct cause. A merely plausible stack trace is not enough.

## Vibes ledger

- **Strong:** source and local call graph describe the same Xe/TTM high-order
  allocation/reclaim mechanism.
- **Strong:** current kernel predates the upstream repair and this NixOS config
  does not declare a backport.
- **Rejected:** free RAM means compaction cannot be the reason for swapping.
