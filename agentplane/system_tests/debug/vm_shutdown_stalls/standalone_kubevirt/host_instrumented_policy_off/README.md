# Host-instrumented policy-off run

This is one uncached, single-attempt policy-off run of the direct-VMI diagnostic. It is separate from the three-run policy A/B recorded in the standalone diagnostic work. The test source and target were an uncommitted patch on the source base identified below. The run also included the merged guest shutdown-helper observer and host virtiofsd process/thread collector, so it is a separately instrumented arm rather than a direct repeat of either A/B arm.

The [BuildBuddy test invocation](https://app.buildbuddy.io/invocation/d08396fd-b354-4530-b482-b6929a1d78a0) ran on runner `06eda718-1c3d-45ce-baa6-9d242e50c2dc`. It failed with `GuestShutdownError` because the guest did not emit shutdown proof; four of the five pytest cases passed. The invocation used:

```bash
nix develop -c bbr test //agentplane/system_tests/vm:test_tcg_vmi_shutdown \
  --test_tag_filters=manual \
  --test_env=AGENTPLANE_TCG_VMI_SKIP_VIRTIOFS_POLICY=1 \
  --runs_per_test=1 --flaky_test_attempts=1 --nocache_test_results
```

The isolated test namespace was `vm-tcg-shutdown-c9ca11f304`. The VMI UID was `02073cab-78c5-48f9-b2c4-7edaed7432de`, and the launcher Pod UID was `04a2d36c-cfa2-4e29-b10e-63545cea277f`. The admission record shows `virtiofs-state` remained an ordinary container because this was the explicit policy-off arm. QMP `query-kvm` reported `enabled:false`; the saved QEMU command line also uses `-accel tcg`. Four guest ACPI request markers were recorded; no QMP `system_powerdown` command count was observed.

## Timeline and observations

- `01:23:12.506Z`: virtiofsd waited for its vhost-user socket; at `01:23:14.755Z` it logged that the client connected and it was servicing requests.
- `01:23:47.377902Z`: the VMI deletion request started. `virt-handler` logged graceful-shutdown signals beginning at `01:23:47.558741Z`.
- `01:23:47.565520Z`: containerd sent SIGTERM to the exact `virtiofs-state` container. It recorded exit status 1 at `01:23:47.607952Z`.
- Guest serial recorded four `AGENTPLANE_GUEST_SHUTDOWN_REQUESTED` markers at `01:23:47.596336Z`, `.606086Z`, `.641655Z`, and `.660663Z`.
- The first guest child snapshot began at `01:23:48.083327Z`. The `systemd-journal` helper was waiting in the FUSE request path. A later snapshot at `01:24:05.280186Z` showed four `sd-sync` processes blocked through `fuse_sync_fs`/`ksys_sync`; `umount` and `journalctl` were blocked in FUSE request paths. The guest still had `state-workspaces.mount` and journal-flush stop jobs pending.
- The host process collector sampled at `01:23:52.808Z` and `01:23:56.740Z`, after containerd had already recorded virtiofsd exit. Both scans found no matching process for the captured launcher Pod UID. These scans cannot show whether virtiofsd had stopped servicing FUSE requests before SIGTERM, and they do not provide a pre-stop thread state.
- A host QMP sample at `01:23:57.065Z` still reported QEMU running. `virt-handler` later reported the domain `Shutoff reason Destroyed` at `01:24:17.800455Z`; containerd recorded the compute-container exit at `01:24:19.815525Z`. This is consistent with the VMI shutdown grace expiring, but the retained logs do not identify the specific signal or force-stop mechanism.

This run directly demonstrates guest FUSE waits while shutdown is incomplete and an earlier containerd SIGTERM/exit for the sidecar. It does not establish that the daemon was already unresponsive when signaled. The Pod status snapshots still said `Running`; that status is not treated as proof of process or request liveness. The direct VMI also lacks the Agentplane Sandbox controller and service stack, so the result is not production-topology parity or proof of the original CI failure's root cause.

## Source and retained bytes

The RBE source snapshot was based on `origin/devel` commit `7a90ed779d002551771bc9bcae62c359e04392b9`. The standalone test change applied to that snapshot is preserved as [`standalone-source.patch.gz`](standalone-source.patch.gz); its decompressed patch SHA-256 is `6313bde20309374456f188ead886aaf7b58d7c0c9bdafc1e02cc201c0716fdbc`, and the resulting test source file SHA-256 was `6d5597908d6187f724dcb96a3cf504af5aeac6ffd9e618730bd8ca2e93312191`. That patch includes the `AGENTPLANE_TCG_VMI_SKIP_VIRTIOFS_POLICY=1` opt-in and is the exact uncommitted standalone test source applied for this run. It matches the test implementation under review in [PR #14693](https://github.com/agentydragon/ducktape/pull/14693).

The source base contains the guest helper observer from [PR #14691](https://github.com/agentydragon/ducktape/pull/14691), commit `9275590b6f`, and the host virtiofsd collector from [PR #14692](https://github.com/agentydragon/ducktape/pull/14692), commit `7a90ed779d`. Those collectors are additional read-only observers relative to the earlier A/B; their presence is part of this arm's source identity.

[`raw_artifacts.tar.gz`](raw_artifacts.tar.gz) contains the unmodified test output files, including guest serial/CRI logs, guest snapshots, host QEMU/virtiofsd snapshots, launcher and virtiofsd logs, containerd and virt-handler logs, lifecycle observations, QEMU command line/QMP state, test XML/logs, and image mapping. [`raw_files.sha256`](raw_files.sha256) lists the SHA-256 hash of every original file inside the archive. The archive SHA-256 is `a9e6e9c44a2abcdba31f79bbc39a4df55880b946a60383e46996fb3422927a37`; the manifest SHA-256 is `79acb8285ac54cb32353b93336e551966cc3498c2bdf347ce7d76821809bf95f`.

The archive preserves the downloaded output bytes; no timestamps, log lines, or payload contents were rewritten. [`SHA256SUMS`](SHA256SUMS) covers the archive, raw-file manifest, and compressed source patch.
