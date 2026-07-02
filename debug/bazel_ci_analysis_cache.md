# Bazel CI Analysis Cache: Speeding Up GHA Builds

## Problem

Even with BuildBuddy remote cache + RBE, GitHub Actions CI time is dominated by
Bazel's **analysis phase**. Analysis runs in-process in the Bazel JVM (Skyframe
graph), can't be remotely cached or executed, and every ephemeral GHA runner
cold-starts a fresh JVM.

## Background: How BuildBuddy Solves This

BuildBuddy runs Bazel inside **Firecracker microVMs** and snapshots the full VM
(memory + disk) after each build. On the next build, the snapshot is restored in
~28ms — the Bazel server resumes with a warm analysis cache. Snapshots are
chunked and stored in the remote cache for sharing across machines. This is their
proprietary "Remote Bazel" product.

Firecracker is AWS's open-source microVM manager (Apache 2.0). Key properties:

- VM-level isolation (separate guest kernel), not just containers
- ~100-300ms cold boot, ~28ms snapshot restore (CoW memory mapping)
- Supports Docker-in-Firecracker
- Snapshots tied to exact Firecracker version + host kernel

## Options Considered

### Option 1: Persistent self-hosted runner (simplest)

Run a plain `actions/runner` binary on a VM (not via ARC) as a systemd service.
The Bazel server stays alive between jobs, keeping Skyframe warm.

- **Effort**: ~30 minutes of setup
- **Pros**: Immediate, no new infrastructure
- **Cons**: Non-ephemeral (dirty workspace between jobs), single point of
  failure, doesn't scale to multiple concurrent jobs
- **Note**: New ARC (`AutoScalingRunnerSet`) is ephemeral-only. Legacy ARC
  supports `ephemeral: false` but is in maintenance mode. A plain runner
  binary outside ARC is simpler.

### Option 2: Firecracker VM snapshots (DIY BuildBuddy)

Fork [Fireactions](https://github.com/hostinger/fireactions) (Hostinger's
open-source GHA runner on Firecracker), add snapshot/restore lifecycle:

1. Run CI job in a Firecracker VM
2. After job: snapshot VM (Firecracker API: `Pause` → `CreateSnapshot`)
3. Store snapshot in object storage, keyed by repo + branch + cache key
4. Before next job: restore matching snapshot instead of cold-booting

- **Effort**: ~2 weeks MVP, 1-3 months production-grade
- **Pros**: Preserves in-memory Skyframe graph, ephemeral semantics (clean env),
  strongest speedup
- **Cons**: Snapshot storage/invalidation is the hard part. Gotchas: clock drift
  (guest clock freezes), entropy re-seeding, network re-init, snapshot
  compatibility across Firecracker/kernel versions.

### Option 3: Skycache (upstream Bazel — not yet available)

Google's internal Blaze has **Skycache**: serializes Skyframe graph to a remote
key-value store and restores it on cold start. Discussed at BazelCon 2025 by
Shahan Yang. Serialization code exists in Bazel, but integration with external
storage is not open-sourced. **No timeline for OSS release.**

### Option 4: BuildBuddy Remote Bazel (paid)

Use BuildBuddy's `bb remote` CLI — GHA job becomes a thin client, Bazel runs
in their Firecracker VMs with warm snapshots. Solves the problem immediately
but is a paid product.

## Open-Source Building Blocks

| Component                 | Project                                                                     | Notes                           |
| ------------------------- | --------------------------------------------------------------------------- | ------------------------------- |
| Firecracker VMM           | [firecracker](https://github.com/firecracker-microvm/firecracker)           | Apache 2.0, mature              |
| GHA runner on Firecracker | [Fireactions](https://github.com/hostinger/fireactions)                     | Ephemeral runners, no snapshots |
| GHA runner on Firecracker | [appsignal/actions-runner](https://github.com/appsignal/actions-runner)     | Simpler, single-org             |
| Container → rootfs        | [firecracker-init-lab](https://github.com/alexellis/firecracker-init-lab)   | OCI image → Firecracker rootfs  |
| Remote cache              | [bazel-remote](https://github.com/buchgr/bazel-remote)                      | Go, single binary               |
| Remote cache + RBE        | [BuildBuddy OSS](https://github.com/buildbuddy-io/buildbuddy)               | Go, includes UI + executors     |
| Remote cache + RBE        | [NativeLink](https://github.com/TraceMachina/nativelink)                    | Rust, FSL-1.1 license           |
| Affected targets          | [bazel-diff](https://github.com/Tinder/bazel-diff)                          | Merkle-hash based; `bazel query` (no analysis) |

## Recommendation

**Quick win**: persistent runner outside ARC (option 1). If the dirty-workspace
tradeoff is unacceptable, the Firecracker snapshot approach (option 2) is the
clean version of the same idea but requires real engineering work.
