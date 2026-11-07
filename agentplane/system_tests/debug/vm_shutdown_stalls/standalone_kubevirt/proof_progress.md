# Guest shutdown proof progress diagnostics

The CI guest's existing shutdown proof checks that `/state/workspaces` and then `/state`
are unmounted before it emits `AGENTPLANE_GUEST_SHUTDOWN_COMPLETE`. The proof now writes
direct-TTY entry, per-mountpoint begin/result, and exit markers around those same checks.
This preserves their order and success/failure behavior; it adds no retry or timeout. The
test parses CRI console records with exact mountpoint boundaries, requires exactly one of
each marker in order, and requires both `findmnt` statuses to be nonzero. It saves
`shutdown-proof-progress.json` even when validation fails.

The independent watchdog delegates a bounded PID 1 child scan to
`guest/shutdown_watchdog_children`. It selects shutdown-relevant processes before the
detail cap, discovers the proof script under PID 1 by an in-memory-only cmdline check, and
walks its child tree to find `findmnt`. It records only PID, `comm`, role, `wchan`, syscall,
and kernel stack; it never writes process arguments to the console. Process-list, tree,
detail, and field sizes are bounded, with explicit incomplete-snapshot markers. The helper
is sampled once per ten-second interval; a healthy fast shutdown can finish before a
post-entry sample occurs.

## Validation

On source base `d6aee79cf2b75b009b4b40a697cac928255b5126`, focused RBE invocation
[`8583bdb8-e51b-46bd-a1cc-e1ba608b23fe`](https://app.buildbuddy.io/invocation/8583bdb8-e51b-46bd-a1cc-e1ba608b23fe)
passed:

- `//agentplane/system_tests/vm:test_shutdown_watchdog_children`
- `//agentplane/system_tests/vm:test_tcg_vmi_shutdown_policy`
- `//agentplane/system_tests/vm:test_tcg_vmi_shutdown_proof_progress`

The helper tests exercise a two-level proof-shell-to-`findmnt` tree, a real Linux shebang
process whose `/proc/<pid>/comm` is the script basename, argument redaction, and an explicit
incomplete result at the traversal bound. The proof parser tests cover both mountpoints,
duplicate/missing markers, and out-of-order markers.

The uncached one-guest smoke passed in
[`309b9154-519f-40ea-af33-d6123fbcb37c`](https://app.buildbuddy.io/invocation/309b9154-519f-40ea-af33-d6123fbcb37c),
using:

```sh
bbr test //agentplane/system_tests/vm:test_tcg_vmi_shutdown \
  --test_tag_filters=manual --nocache_test_results \
  --flaky_test_attempts=1 --runs_per_test=1
```

The retained progress report had no errors and placed all seven proof markers consecutively
in the expected order. Both mount checks returned status `1`, followed by the existing
shutdown-complete receipt and proof exit status `0`. The retained shutdown diagnostic
classified the one guest as `orderly_guest_powerdown`; its serial ended with `reboot: Power
down`, and correlated handler logs recorded `Domain is in state Shutoff reason Shutdown`
with no grace-expired or launcher-kill event. The ACPI handler receipt appeared four
times. The watchdog's first sample occurred before the proof helper was present and marked
two already-exited children as `comm_unavailable`; it did not capture a live `findmnt`
stack in this healthy run.

An earlier smoke,
[`c5cf48ae-1087-406a-9746-be62dd8d5304`](https://app.buildbuddy.io/invocation/c5cf48ae-1087-406a-9746-be62dd8d5304),
completed the same guest shutdown but failed a diagnostic assertion: prefix matching for
`/state` also matched `/state/workspaces`. The retained console showed both status `1`
results, complete proof, and successful powerdown. Exact marker matching and the focused
regression tests above corrected that assertion; it was not a guest shutdown failure.

## Limits

This uses the real CI guest and real guest shutdown units under a direct VMI with KubeVirt,
the namespace-scoped virtiofs native-sidecar policy, a PVC-backed `/state`, and the nested
workspace disk. It does not run the full Sandbox controller, Agentplane application,
session, or runner-service topology. One healthy shutdown validates that the new diagnostics
do not prevent this path from completing; it neither reproduces the intermittent stall nor
establishes its cause. The watchdog can miss a short-lived helper, and a console write can
itself block, so absent markers or samples are missing evidence rather than proof that an
operation never ran.
