# Private systemd proof in the existing direct guest

Keep the full required VM and OOM acceptance while testing the next manual
candidate graph. The direct Claude guest already passes the unchanged real
systemd fixture, including all 11 private subtests, within its existing boot.
The same-source omission control at `fa9094cd8a` fails the unchanged receipt
assertion when only its kernel argument is omitted. The earlier
[private proof](test_architecture_direct_private_systemd.json) retains that pair.

The new opt-in `VM_PLATFORM_PRIVATE_SUPERVISION_OWNER=direct-guest` selects the
ordinary VM template in the manual platform witness. Default `deployed` retains
the supervision template and private receipt requirement. Both modes read the
actual VMI through its exact launcher Pod owner, compare owner UIDs and check
the supervision kernel argument against the selected owner. The direct-owner
mode also rejects an unexpected private receipt from the deployed VM. Unknown
owner values fail; no production setting or required selector changes.

All platform setup, process/aggregate/service kernel budgets, workspace, issuer
and Pod identity, Runtime lifecycle, CLI routes, detached-descendant fencing,
same-boot and cleanup assertions remain. This removes only duplicate private
preflight from the experimental deployed witness. The direct guest still owns
the real fixture and its receipt; its executor budget remains disabled, while
the private fixture uses the same process/aggregate budgets and block devices.
The deployed Codex setup retains its existing executor configuration and kernel
assertions. This division does not replace deployed OOM enforcement.

## Bounded comparison

Run these two targets together, uncached and with one flaky attempt, in each arm:

```text
//agentplane/system_tests/suites:test_vm_platform_light_guest_candidate
//agentplane/x/direct_vm_boot:test_runtime_session
```

A uses `VM_PLATFORM_PRIVATE_SUPERVISION_OWNER=deployed`; B uses `direct-guest`.
Use the same source, reduced deployed root, direct guest root and stack inputs.
Start with one serial A/B pair. Preserve native invocation and NEW physical
execution identities, nonempty JUnit cases, fixture compatibility keys, import
exposure, readiness, teardown, CPU and both private-proof receipts. Record any
failure before considering a repeat. Counterbalance only if the first pair
shows useful possible benefit and unmatched cache exposure requires it.

The two-target graph establishes whether proof placement removes work or moves
its tail. It does not establish complete required-feedback savings. In the
historical `376f8ae` screen, MCP Services was not the last target in any arm;
its closest gap was 4.184 seconds behind the deployed witness in B2. That gap
is headroom for shortening the VM tail before Services becomes limiting; a
Services-only improvement saves zero completion time in all four observed arms.
Pod remains another competing tail. A promising local result still needs the
complete fixed-source graph, including all selected targets, before expanding
the qualification cohort. Reject or defer this change if the work reduction is
small, moved into another tail, or bought by worse cleanup/reliability or cost.

Launcher/mount and issuer/Pod-binding controls against the new composition
remain necessary before any gate promotion. No required owner or frequency has
moved. Results and the next worthwhile probe remain tracked in
[#16099](https://github.com/agentydragon/ducktape/issues/16099).

## Rebase validation and recovery-prefix race

The branch rebased onto devel `a4785875d1` at `44a0c04d7f` without conflicts.
A full tree audit preserves all 105 branch paths and 85 upstream paths, with no
unexpected tree change. Fresh six-target validation
[`77d101be`](https://app.buildbuddy.io/invocation/77d101be-a24d-49d1-91b6-8755b2034154)
has six NEW physical actions and nine JUnit cases: eight pass and one recovery
case fails because a late harness event arrives during Runtime shutdown after
the test captured History. The failure is retained, not counted as a passing
retry. All other manual targets pass, including both packaged harnesses.

At `a49291d92b`, the recovery test reads the exact previously observed prefix
length and retains event equality, restart inventory, deduplication and next
input. Fresh healthy
[`85868ecb`](https://app.buildbuddy.io/invocation/85868ecb-8ec3-49a7-bfc7-de246a5052e4)
passes all four cases with no errors/skips. The same-source discarded-journal
control
[`061c7aec`](https://app.buildbuddy.io/invocation/061c7aec-a160-4458-964e-ef7ac95412a7)
passes the repaired prefix check and fails the intended missing retained-Session
assertion. Each is one NEW physical action; the control selects one case and
intentionally deselects the other three. These validate the repair and sensitivity,
not a speedup or a low flake rate. Raw test artifacts remain private.
