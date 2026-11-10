# Five concurrent full-stack shutdown diagnostics

On 2026-11-07 UTC, an uncached five-run batch of the manual one-guest full-stack diagnostic ran on [BuildBuddy invocation `94004fd5-bd17-4eb8-9df6-a66ce01804a4`](https://app.buildbuddy.io/invocation/94004fd5-bd17-4eb8-9df6-a66ce01804a4). All five remote test actions passed on their first attempt; none reproduced the late post-ACPI stall. The [outer runner invocation `bf3dd38c-0a1e-4891-9458-51bf0d967502`](https://app.buildbuddy.io/invocation/bf3dd38c-0a1e-4891-9458-51bf0d967502) fetched and checked out exact PR #14710 head `203b57b9c0d14f811d47e1143785fe9abf4b4763` without applying a working-tree patch. Its parent is `d3fac3a404e3447861d6d42d40da6d4ef7d59bc6`; its ancestry includes the merged proof-marker change `675d6841213bca24f543289e83e536ab6c3568eb` (#14712). At launch, `origin/devel` was `6d043cf82ddae7340d66a71ff592680b19e97ad3`; the PR-head merge base was `9909d231f3b640b680974411231d632a399806bd` and the diff from that base was 12 files, 669 insertions, 19 deletions. The runner's source checkout began at 03:32:53 UTC and completed at 03:32:55 UTC.

The command was:

```sh
bbr --run_from_branch=vm-shutdown-virtiofs-timeline \
  --run_from_commit=203b57b9c0d14f811d47e1143785fe9abf4b4763 \
  test //agentplane/system_tests/suites:test_vm_shutdown_diagnostic \
  --runs_per_test=5 --flaky_test_attempts=1 --nocache_test_results \
  --test_tag_filters=manual --test_env=AGENTPLANE_VM_VIRTIOFSD_TIMELINE=1 \
  --test_output=errors --jobs=5
```

The BuildBuddy test summary records five runs, five attempts, zero cached results, and five passes. All five independent RBE actions overlapped from 03:33:49.706 to 03:36:41.729 UTC. Each action started one disposable cluster and one guest; the following times are BuildBuddy TestResult action times on 2026-11-07 UTC.

| Run | Start–end UTC             | VMI UID                                | ACPI receipts | Post-ACPI samples | Result                             |
| --- | ------------------------- | -------------------------------------- | ------------: | ----------------: | ---------------------------------- |
| 1   | 03:33:36.147–03:36:47.379 | `5f56abb8-a974-4051-ba60-103c789005b1` |             4 |                 4 | Orderly powerdown; sandbox deleted |
| 2   | 03:33:36.147–03:37:27.948 | `1f6eacd0-73c1-4a72-b9d7-c6db4f6fe24c` |             4 |                 5 | Orderly powerdown; sandbox deleted |
| 3   | 03:33:36.147–03:37:23.271 | `79a7152e-47c9-4b7b-ac92-467949c84dc2` |             4 |                 5 | Orderly powerdown; sandbox deleted |
| 4   | 03:33:36.147–03:36:41.729 | `f34eba6d-f6a1-4322-979f-34907d50fdc4` |             3 |                 4 | Orderly powerdown; sandbox deleted |
| 5   | 03:33:49.706–03:37:25.211 | `90af848f-fc78-46dd-b2ee-ddc799a4f7b2` |             4 |                 5 | Orderly powerdown; sandbox deleted |

Each raw serial has, in order, `AGENTPLANE_SHUTDOWN_PROOF_ENTER`, `/state/workspaces` and `/state` check begin/result with `findmnt_status=1`, `AGENTPLANE_GUEST_SHUTDOWN_COMPLETE`, `AGENTPLANE_SHUTDOWN_PROOF_EXIT status=0`, and `reboot: Power down`. The UID-correlated host report has one `Shutoff reason Shutdown` record per guest and no grace-period expiry, launcher kill, or destroyed-domain event. The passive host timeline found all three named virtiofsd daemons at the ready-time baseline; each summary ended with `stop_reason=proof-and-daemon-absent` after two complete scans without a matching daemon. QMP was disabled for this optional local-proc-only observer. A process snapshot does not prove it was servicing FUSE requests.

[The archive](raw_diagnostics.tar.gz) contains 53 unchanged raw files (1,985,064 bytes before compression; 282,499-byte archive): five serials, each run's shutdown report and progress JSON, and all host virtiofsd snapshots, target metadata and timeline summaries. The archive contains a `SHA256SUMS` manifest for every retained file; [the outer checksum](raw_diagnostics.tar.gz.sha256) verifies the archive itself. Run `sha256sum -c raw_diagnostics.tar.gz.sha256`, then extract it and run `sha256sum -c SHA256SUMS` in the extraction directory. Both checks passed before publication.

The original BuildBuddy test outputs also contain five 8 MiB binary guest journals and 45 other files: test logs, CRI console/handler/launcher/relay/virtiofs logs, and launcher topology. Those 50 files (45,629,759 bytes) are omitted from this repository archive to limit retention of incidental service output; they remain in the linked invocation. A scan of the retained raw bytes found no private-key block, authentication header, JWT-shaped token, AWS access key, key/password assignment, SOPS age key, or Kubernetes Secret object. The archive excludes BuildBuddy command logs, which can contain credential-bearing options. This bounded healthy batch tests the observer and shutdown path under concurrent RBE load, but does not identify or rule out the historical intermittent blocker.
