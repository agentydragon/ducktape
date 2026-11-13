# Packaged guest milestone and required CI

Source `bedb9a56619abf49518810174ff43e21a8548677` is the pushed guest-supervision
and read-only-mount milestone on architecture PR #16102. All three required
checks pass in [workflow 43489485567](https://github.com/agentydragon/ducktape/actions/runs/43489485567).
The clock from earliest required workflow creation to last required completion is
678 seconds. Native [87c1a755](https://app.buildbuddy.io/invocation/87c1a755-4e29-5fb9-8ab1-88867a323197)
takes 575.864 seconds. Its 118 unique NEW physical TestRunner actions cover 93
fresh target labels and 786 JUnit cases: no failures, errors, skips, merged records
or failed physical records. Thirteen K3s suite actions execute freshly. The
compound VM case remains one JUnit case containing two owned harness scenarios;
it is not two independent reliability trials. The eight Bridge shards run the
same 24 canonical cases as the pinned shard probe.

Allowlisted metrics, clocks, immutable merge parents and actual case denominators
are in [the required-CI evidence](test_architecture_required_bedb9.json). Ordinary
cached gate results remain valid but are excluded from fresh-exposure counts.

## Why this is not a timing comparison with the previous head

The earlier source `8a555b2` required workflow took 351 seconds, with 59 NEW physical
TestRunner actions and no fresh K3s suites. The two workflows tested different
merge trees. Prior merge `c8d36b7` has parents `f2a98e9` and `8a555b2`; this merge
`6f7aa18` has parents `5fa72bb` and `bedb9a5`. The actual tested base advanced
13 commits. GitHub's PR base field still projected the older base, so that field
alone is not authoritative provenance for the required run.

Read-only CAS inspection of unchanged App `test_history_gateway` TestRunner and
mypy actions finds identical Command digests but different input roots. A concrete
changed blob is `integration_fixtures.py`: the executable runfiles path moves
from `app/go/agentplane-app_` to `app/cmd/agentplane-app_`, matching the newer tested
merge tree. The newer base also changes direct deployment-renderer source inputs.
The earlier render action is cached and unavailable for a complete per-action
key comparison. These observations explain legitimate input changes; they do not
attribute every cache miss to one commit, prove runner contamination, or show
that the guest helper invalidated unrelated tests.

## Coverage and follow-ups

The shared supervision helper passes in a fresh direct Claude Session and the full
deployed VM scenario. The real wrong-History-URI control fails the ordinary
History assertion after healthy-service mTLS preflight and a real Shell command.
The full VM validation also preserves a managed action-VM scheduler panic followed
by a passing automatic retry. See [the guest evidence](test_architecture_guest_session.md)
and [the managed-kernel investigation](../../../devinfra/buildbuddy/debug/2026_11_02_scheduler_kernel_panic.md).
The passing required invocation above does not establish a recurrence-rate fix.

The [read-only mount screen](2026_11_13_guest_mounts.md) is adopted only in the
manual launcher. [Bridge bytecode](../../app/debug/2026_11_13_bridge_bytecode.md)
stays off because cold/full-workload cost remains unqualified. The published
[iteration 9 reports](https://agentydragon.github.io/ducktape/agentplane-test-architecture/)
remain explicitly pinned to the earlier `8a555b2` checkpoint; they are historical
measurement records, not this source head's qualification.

The manual direct Codex/executor witness now passes alongside Gateway and Claude:
[14346e74](https://app.buildbuddy.io/invocation/14346e74-5c19-4ba6-adb5-e78cfaeaf2aa)
has three NEW physical test actions, three passing JUnit cases and no retries or
merged executions. Gateway, Claude and Codex TestResult times are 84.8, 128.5 and
175.4 seconds respectively;
the 212.380-second invocation is not a comparison with the deployed VM suite.
The tested source is `bedb9a5` plus local patch SHA-256
`02786509a5f19369702eb3ebe5d41b3f568147591073c750f4c7a67a856e2e68`.

Codex uses the current packaged CLI, the production VM executor and its real leaf
cgroup limits. Two Shell calls write and read a fresh workspace marker; both
command IDs and completed turns remain in contiguous, sourced History under one
Runtime boot. UID 107, PID 1, executor slice and systemd privilege denial are
required. Reading the limits establishes configuration, not actual OOM enforcement.
The existing deployed VM and OOM cases retain their ownership.

The [negative controls](https://app.buildbuddy.io/invocation/1e6561d6-fc68-4601-9d49-e0e08ffa4441)
are two NEW actions and two expected failures, not healthy-run reliability exposure.
Removing the executor makes the real command run in `agentplane-workloads.slice`;
the unchanged executor-slice assertion rejects it. The wrong-History-URI control
still reaches healthy History through both mTLS relay legs, then Runtime rejects
the substituted service URI and the ordinary History completion assertion fails.
One earlier control submission accidentally disabled the local patch with
`run_from` flags: it failed target lookup before any physical action and is
excluded from these control counts.

Earlier Codex attempts remain failures: a constructor type-check failure, a wait
that outlived QEMU, then a native startup failure with systemd `200/CHDIR`.
The launcher now observes guest exit throughout the contract. Its state daemon
runs unprivileged as production does, so guest-root-created private Codex state
stays accessible to UID 107; merely changing the exported directory owner did not
model that creation behavior. The first healthy three-target run above includes
this repair. Subsequent source cleanup only removes import aliases and documents
the manual contract; exact pushed-head CI must still validate the final milestone.

Egress and Notification CLI ownership still crosses real sidecar/SVID routing; an empty
Notification list alone does not prove the Sandbox resolved. A future focused
composition should seed an owned subscription and verify revocation against real
services, preserving deployed issuer, launcher relay, mount and identity checks.

A published internal-service image baseline currently lacks a test-side read-auth
path to private Forgejo images. Do not pass publishing credentials into PR actions.
A shadow baseline needs immutable image/source proof and API compatibility keys;
it does not justify freezing current guest or Runtime artifacts. Required deployment
tests and assertion frequency remain unchanged. Complete architecture replays,
busy-window and burst exposure, and matched rerun-inclusive required clocks remain
before promotion or a diminishing-returns conclusion.
