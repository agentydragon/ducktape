# Offline VM failure watch

This is a manual BuildBuddy review. It does not add a CI job or a continuously
running watcher. The guest shutdown watchdog, SysRq worker and host collectors
are already part of the VM test runtime; their outputs can be inspected when a
new failure is retained. Raw artifacts stay in BuildBuddy and may expire.

## Snapshot on 2026-11-08 at 00:48 UTC

The indexed history for `//agentplane/system_tests/suites:test_vm` over the
preceding 24 hours returned 125 rows: 122 passed and three failed. The flake
sample query returned the same three failing attempts; none began after the
default SysRq change merged on November 7. This is a single-target snapshot,
not an exhaustive search of all Agentplane tests or all unindexed executions.
One history row is not necessarily one execution: for example,
[invocation `86a794ae`](https://app.buildbuddy.io/invocation/86a794ae-d9c1-5ff1-9230-be572e184631)
reports `cachedLocally: true` for this target. Consequently these counts do not
establish a failure rate.

The latest failed attempt, [invocation `79acbe48`](https://app.buildbuddy.io/invocation/79acbe48-12c4-579a-a163-3c8ab29771a9),
run 1, shard 1, attempt 1, started at 17:06:01 UTC on November 7, before
SysRq was enabled by default. The `test_vm_scenarios` assertion expected a
particular OOM command outcome but observed exit 143. Its three guest consoles
each include `AGENTPLANE_GUEST_SHUTDOWN_COMPLETE` followed by kernel
`reboot: Power down`. Thus this attempt is not evidence of the missing-proof,
post-ACPI shutdown stall. It retains consoles, raw serial, host QEMU samples,
launcher and handler logs, and native journals under `test.outputs/vm-artifacts/`.
The serial paths identify these VMI UIDs:

| Guest                  | VMI UID                                | Launcher pod UID                       |
| ---------------------- | -------------------------------------- | -------------------------------------- |
| `systest-vm-1`         | `b3d43f1a-9523-4638-a290-e089f28aa0c6` | `f9185b5b-409b-4e5f-9e30-e814e3d39d0d` |
| `systest-vm-2`         | `65220bc0-b096-480b-a56f-326c58b74f9d` | `d5e3eaaf-9de1-42e3-9315-96b81d9920c4` |
| `systest-vm-codex-oom` | `7600ba23-09cc-439a-aac9-51cb23eea579` | `63a4058b-0880-4368-9cd6-df8f5cb82b3e` |

The other two failed attempts, [`4789e905`](https://app.buildbuddy.io/invocation/4789e905-f7c7-5cc3-b41a-796ae236dba4)
and [`ee8b4d09`](https://app.buildbuddy.io/invocation/ee8b4d09-93de-5b36-bbb6-8aa03c2da3bb),
are the previously catalogued QEMU boot-assertion cases, not post-ACPI stalls;
see [the historical census](historical_census.md).

## Repeat the review

Use the Nix devshell and authenticated `bbapi`. Set the review interval to
cover the time since the previous inspection, with overlap for indexing lag:

```sh
bbapi target history --repo https://github.com/agentydragon/ducktape \
  --label //agentplane/system_tests/suites:test_vm --since 24h --count 200 --json
bbapi target flakes //agentplane/system_tests/suites:test_vm \
  --repo https://github.com/agentydragon/ducktape --since 24h --json
bbapi target <invocation-id> --label //agentplane/system_tests/suites:test_vm --json
bbapi artifact list <invocation-id> --kind test --failed --json
bbapi target log <invocation-id> //agentplane/system_tests/suites:test_vm --failed
```

Narrow the time window or account for pagination if the history response is
truncated. Inspect each failed run, shard and attempt separately, including
failures hidden by a retry. Resolve wrapper invocations to their child and
record the invocation, configuration, run/shard/attempt, source commit, VMI and
launcher identities. Check cache fields before describing a result as fresh.
Inspect that guest's raw serial and console alongside UID-correlated handler
and launcher events; a missing proof alone can also be caused by a boot failure,
missing artifact or receipt parsing. Record SysRq markers, blocked-task stacks,
shutdown progress and host snapshots if present. Absence of a dump does not
prove that the guest was not blocked: the worker and console must make progress
before the host grace period expires. The diagnostic collection and its limits
are described in [the SysRq report](standalone_kubevirt/sysrq_default_ci.md).

The invocation link and artifact names are references, not a durable copy of
the evidence. If a new stall occurs, preserve the relevant files promptly and
review their contents before publishing them.
