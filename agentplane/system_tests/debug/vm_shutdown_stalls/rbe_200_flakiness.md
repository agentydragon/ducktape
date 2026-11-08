# Flakiness encountered in the 200-run VM experiment

This is a snapshot taken on 2026-11-08 at approximately 17:52 UTC. It records
different failures encountered while running the [serial VM experiment](rbe_200_run_job.md);
it does not assert a shared root cause. The source commit was
`f2b019861e975fc2a63b656b339a7f201b4066d1`, the test target was
`//agentplane/system_tests/suites:test_vm`, and test-result caching and Bazel
flaky-test retries were disabled. Execution-provider retries are separate.

At this snapshot, 40 complete test results from the initial attempt and ten
verified continuation batches comprised 39 passes and one failure; two more
passing results were visible in the active next batch. Counts include the
tenth initial test, recovered from its execution result after the parent runner
stopped, rather than from the incomplete Bazel test summary.

## Additional failures at 67 completed runs

A later snapshot recorded 67 verified results: 64 passed and three failed. The
active Job had submitted the next batch. These are whole-suite results, not an
estimate of a common failure mechanism.

[Invocation `331d8d98`](https://app.buildbuddy.io/invocation/331d8d98-b806-415c-b9f3-55169dbe1a4d),
run 1 of 3, passed the test scenario but failed teardown with
`GuestShutdownError`: the `systest-vm-codex-oom` launcher did not produce the
shutdown-complete marker. The launcher signaled graceful shutdown at 20:04:52
UTC and recorded the domain destroyed at 20:05:23. The guest console shows
systemd performing shutdown, with messages still progressing shortly before
destruction; this establishes an incomplete shutdown within the grace period,
not a proven deadlock or the cause of earlier stalls.

The guest watchdog armed SysRq and successfully issued `w` at guest uptime
225.15 seconds; the recorded blocked-task dump contains no task stacks. A
snapshot subsequently recorded PID 1 in `do_epoll_wait`, while systemd was
still processing shutdown jobs. These are point-in-time observations, not proof
of the process responsible for the delay. No RCU stall report or shutdown-complete
marker appeared in the affected guest serial log. The later host report could
not inspect QEMU because the launcher pod had already disappeared; earlier
host snapshots and the raw console remain in the invocation's artifacts.

[Invocation `3e01a55d`](https://app.buildbuddy.io/invocation/3e01a55d-f7ba-40ca-a174-a733113a662c),
run 3 of 3, failed before scenario completion: `systest-vm-1` did not become
Ready within 300 seconds. Its workload pod was Ready, but the Runner Gateway
was still waiting for the runner's first state message. This does not by itself
establish a guest RCU stall or a relationship to the shutdown failure.

## Guest did not become Ready; RCU reports and forced cleanup

In [invocation `1c1dc380`](https://app.buildbuddy.io/invocation/1c1dc380-f62b-48f4-8925-e9eea2c11347),
run 2, shard 1, attempt 1, configuration
`eef0f2a585dffd4fcf3bfc821929e25032c977c2b95a55f44e8c4bcbb2323dd2`,
`systest-vm-codex-oom` did not become Ready within 300 seconds. Its VMI UID was
`822bc372-1f45-451e-96aa-990e7e6d893f` and launcher Pod UID was
`5d089109-6e9d-42f8-bc8b-318293900281`.

Its raw serial and timestamped console reported repeated `rcu_preempt` stalls
starting at 07:26:19 UTC on the Debian 6.12.115 guest kernel. The first report
showed CPU 1 in `pv_native_safe_halt` and mentioned a possible timer-handling
issue. At 07:30:27 the UID-correlated virt-handler signaled graceful shutdown;
at 07:30:57 it reported grace expiry and the domain was destroyed. Neither
the raw serial nor CRI console recorded the guest's ACPI receipt, shutdown
proof, powerdown, or SysRq marker. The other two guests recorded proof and
powerdown. This resembles the previously observed pre-Ready RCU wedge; these
observations do not identify its cause or show that it caused the historical
post-ACPI stall. The test log, guest consoles, raw serials, host snapshots,
launcher and handler logs remain available as test artifacts in that
invocation, subject to retention.

## GitHub dependency fetch returned 504

[Invocation `823c6c31`](https://app.buildbuddy.io/invocation/823c6c31-0892-44d1-889c-e308b257743d)
failed at 08:55 UTC during Bazel analysis when fetching
`https://github.com/astral-sh/uv/releases/download/0.12.5/dist-manifest.json`
returned HTTP 504. BuildBuddy recorded a completed `BUILD_FAILURE`, Bazel
reported zero test targets, and the execution API returned no TestRunner
actions. No VM test result was counted for this invocation. The Job stopped
without replay, and a new Job resumed from the 40 verified test results.

The continuation has a bounded retry only when this same uv-manifest 504,
zero-target log, completed build failure, and absence of TestRunner executions
are all observed. Other incomplete outcomes still stop without automatic replay.
The 504 establishes a failed GitHub download, not which upstream network
component caused it or how often it happens.

## BuildBuddy execution-VM panics hidden by successful retries

BuildBuddy's execution API showed more TestRunner execution records than test
results in three otherwise passing batches. In each, the initial execution
returned `UNAVAILABLE` after the worker's Firecracker socket refused a pause
request; a separate RBE execution of that same test action completed, and the
Bazel test result passed. All three failed executions retain a
`vm_log_tail.txt` server log showing a Linux 5.15.0 worker kernel panic. These
are the outer execution VMs hosting k3s and QEMU, not the inner Agentplane
guests. The retries are not extra independent passing test results.

| BuildBuddy invocation                                                                   | Test run | Failed execution ID                                                                                                         | Retained VM log digest/size                                              | Panic location                        |
| --------------------------------------------------------------------------------------- | -------- | --------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | ------------------------------------- |
| [`0e141b5d`](https://app.buildbuddy.io/invocation/0e141b5d-fa5b-4443-ac8f-89d25d33c6f4) | 3 of 3   | `/uploads/739cdb21-591c-4056-9526-33e32453aca2/blobs/9d3e5a2d3ae8905fb4cd63dd03c61fa692321cda9205fca232872749a5911c9f/1852` | `43124e9e05f87c4b0af6f8fdfee43d2f2797ea997fba8ff2b005044b1aade7a7/22239` | `update_blocked_averages+0x273/0x740` |
| [`78bf295c`](https://app.buildbuddy.io/invocation/78bf295c-421f-4556-98d2-82bcfa086f13) | 3 of 3   | `/uploads/96849a47-d05c-497f-b859-9bc563f38d41/blobs/9d3e5a2d3ae8905fb4cd63dd03c61fa692321cda9205fca232872749a5911c9f/1852` | `b93c3eff68fd1db7ef0a93a824cfc5b56f94c91de0593d055207eabb616329ac/17705` | `update_load_avg+0x92/0x5c0`          |
| [`21ee390d`](https://app.buildbuddy.io/invocation/21ee390d-852d-42c5-891e-fc69124a4554) | 1 of 3   | `/uploads/e5959424-8870-4f25-9886-727af40a6416/blobs/8de5edec4fe44a34cbfd67688bef911d3d9484cd411b40d9975550e962165f55/1852` | `6f25876fc323199e71ae99681f48b92445919e4c3b78a14b2a6d804141c092df/19863` | `update_load_avg+0x92/0x5c0`          |

All three traces reported a general-protection fault on a noncanonical address.
These match locations observed in earlier RBE failures in the [main report](README.md),
but do not identify the corruption writer or prove a relationship to the guest
RCU stall. Recover the failed execution metadata with `bb execution get
<execution-id> --output=json` and its `serverLogs.vm_log_tail.txt` blob with
`bb download <digest>/<size>`. These are BuildBuddy CAS references and can expire.

## Hosted-runner limit and client bootstrap

The original [outer runner](https://app.buildbuddy.io/invocation/3d9edcc4-f972-4809-b4b9-823508bb9221)
was stopped at the one-hour free-tier limit despite the requested 48-hour timeout.
Its tenth test action continued and passed after that stop; the [experiment
report](rbe_200_run_job.md) records the independent test XML and execution ID.
This is a known limit discovered during the experiment, not a VM shutdown flake.

An earlier Nix-based client bootstrap also failed because its container lacked
the standard native loader for downloaded binaries. It did not submit a
BuildBuddy invocation. The active Job uses the pinned BuildBuddy runner image
and `bb remote`; the bootstrap failure is an experiment setup issue, not
evidence of a CI or guest defect.
