# One deployed VM without model turns

The manual `//agentplane/system_tests/suites:test_vm_platform_candidate` now
checks one real KubeVirt guest through ordinary Session setup. It uses existing
App/Dispatcher authorization; the VM stack does not admit Executor RPC callers,
so this experiment adds no service, caller privilege or execution API.

It retains the real controller, issuer, launcher and mounts, private systemd
supervision receipt, kernel limits and unprivileged setup ownership. A unique
workspace file is written, read and removed. Real CLI calls verify Egress
Identity and Notification routing. A second setup verifies detached descendants
were fenced; authenticated Runtime status and Pod/credential identity must
remain on the same boot. Any upstream model request fails the case. Both packaged
guest contracts still own their two-turn, harness and exact History assertions;
the required full VM suite and separate OOM acceptance remain unchanged.

## First physical proof

[Invocation 2e5581f3](https://app.buildbuddy.io/invocation/2e5581f3-348d-4b3b-a75f-398cf5780f5a)
passes: one NEW physical TestRunner, one nonempty passing JUnit case, no
failures, errors, skips, model requests or test retries. The source was
`ff39339c927c94a26c028cc69f4a5b55efc977c6` plus the staged two-file patch with
SHA-256 `9f28f6ea91b07a0862620903058940ffc203a9326e25d0ae3b4e1fc1911f5ce2`.
That patch adds only the candidate target and its test body; it is preserved in
the subsequent commit. Native invocation elapsed is 403.285s, physical test
execution 356.510s, and reported test CPU 1,099.554s. Cleanup completes and
releases one workspace loop device.

| Observed phase                             | Seconds |
| ------------------------------------------ | ------: |
| Cold stack setup                           | 109.098 |
| Case span, including owned Sandbox cleanup | 152.462 |
| Sandbox readiness within the case          | 112.454 |
| First setup output wait                    |  14.595 |
| Fence setup output wait                    |   2.107 |
| Owned Sandbox cleanup                      |  17.866 |
| Final stack cleanup                        |  30.831 |

Phases are nested and overlap; summing them is invalid. JUnit's 298.430s includes
fixture phases and is not the case-body clock. This cold run uses compatibility
fingerprint `ac4cf98674a1ff7ed4ea239c6a3f37e5e6c8070822b977708b97c4bf98bc4d93`.
Seven non-skipped imports total 74.054s, including a 42.498s guest-image import.
The earlier two-guest candidate and full VM measurements are different source
and cache exposures. This single pass establishes neither a speed saving nor
coverage equivalence, p90 or a reliability rate.

## Readiness audit and image screen

Use the existing boot/launcher/Runtime diagnostics to explain readiness before
expanding expensive comparisons. In this run the readiness span begins at
00:11:13.376Z, the launcher reports domain start at 00:11:46.252Z, the console
reports the private supervision pass at 00:12:55.644Z, and readiness returns at
00:13:05.830Z. These are cross-process observations, not an exact additive
breakdown of kernel or controller CPU. The retained journal narrows this down:
guest startup reports 17.229s kernel plus 24.715s userspace; the private Go
fixture itself takes 19.830s and contains 11 subtests. Runtime's systemd unit
starts its wrapper before that fixture, so unit `Started` does not mean Runtime
is serving. The roughly 33s before domain start includes scheduling, container
initialization and launcher startup. There is no duplicate fixed wait in the
workspace/home prepare unit ordering. The roughly 7.4s between Runtime's
WatchState start and first state has no causal subspans; its owner is unresolved.

The manual `test_vm_platform_light_guest_candidate` uses the same body and
rendered image reference with a separate root containing Codex but no Claude.
The ordinary and profiling roots retain both CLIs. At literal source
`cdad46e75623a058c9bb19aaac7147c840542cca`, the reduced root is
408,961,024 bytes versus 553,885,696 bytes: **144,924,672 bytes (26.2%) less**.
Downloaded bytes match the native build's advertised SHA-256 identities.

| One serial B/A screen   | Full root A | Reduced root B |
| ----------------------- | ----------: | -------------: |
| Native elapsed          |    439.855s |       411.886s |
| Physical test execution |    277.495s |       266.409s |
| Reported test CPU       |    798.615s |       538.157s |
| Guest image import      |     43.766s |        20.116s |
| Stack setup             |     60.398s |        35.436s |
| Sandbox readiness       |     99.747s |       136.646s |
| Case span               |    139.907s |       168.251s |

[Full A](https://app.buildbuddy.io/invocation/c0214fb8-fdc9-4a6b-aded-90e1d95a7f1e)
and [reduced B](https://app.buildbuddy.io/invocation/1d9fe58a-84bb-4867-94dd-904407c843ed)
each pass one nonempty JUnit case in one NEW physical action, without skips,
errors or test retries. Both use a warm compatible baseline, import the changed
root, report `layers_in_memory=false`, complete cleanup and release one loop
device. The import drops 23.650s; total physical execution drops only 11.086s
(4.0%). Readiness varies against the reduced image. These overlapping phases,
different workers and one serial pair do not establish a complete-CI saving,
p90, a reliability rate or a causal CPU reduction.

The first attempted image removed both CLIs and 75.4% of root bytes.
[It fails readiness](https://app.buildbuddy.io/invocation/77131353-d653-42c5-ae07-7fc064d0dc53):
embedded Execution still launches Codex without model turns, and its native
service exits with systemd status 203. Restoring Codex produces the passing
variant above. The rejected image's size reduction is not a usable saving.
The first required layout check also failed because `dump.erofs --path` returns
zero for a missing inode; the corrected assertion checks its actual diagnostic
and empty output. All three required checks pass at `cdad46e756`, including
three nonempty layout cases and the full VM case; native required invocation
`f36e7e40-79d6-5dce-a329-be8736e1ea8e` tests synthetic merge `107f9241bf`.
This is correctness evidence, not another architecture timing pair.

[Reviewed image identities, phases and rejected attempt](test_architecture_platform_image_screen.json)
retain the source, native action and cache provenance. The smaller root is useful
only for this platform composition; it cannot replace a two-harness gate.

## Private systemd proof in the existing direct guest

The existing direct Session guest already carries the same private systemd
fixture binary and root. At pre-rebase source `4ff7cf51e6`,
[the opt-in proof passes](https://app.buildbuddy.io/invocation/6112d945-2813-4f3c-8a38-86b72a0d561e)
in that same guest: one NEW physical action, one nonempty JUnit pass, both
packaged Claude turns, exact History, setup/fence, real CLI routes, credential
revocation and cleanup. The native Go parent passes in 17.35s with 11 child
passes; its exact private receipt appears on this guest's serial before
authenticated readiness. The result file is also checked after teardown restores
the host owner. The existing 120s QEMU cap is unchanged, and no extra VM or
required target was added.

Native elapsed is 224.093s, physical test execution 188.694s and reported test
CPU 309.784s. These are a single correctness proof, not a matched saving.
The first host-side assertion failed with `PermissionError` even though the
guest's private checks passed: live guest state is deliberately private. The
repair keeps the serial receipt check before readiness and reads the file after
ownership restoration; it changes no permissions. The failed full test remains
separate from the successful guest subtest result.

[Reviewed direct proof](test_architecture_direct_private_systemd.json) retains
both attempts. Before removing the duplicate private checks from the manual
deployed platform candidate, seed controls against the changed composition and
assign the direct target its required-graph owner. No required owner has moved.

At literal source `fa9094cd8ad82ec9ac4f7496ab864e50cf9767f3`, the same-source
[healthy run](https://app.buildbuddy.io/invocation/6cb56451-51b4-4719-ac77-9f1b4e00ba91)
passes all original assertions in one NEW physical action, with one nonempty
JUnit pass and no errors or skips. Its private Go parent passes in 12.15s with
11 child passes. The opt-in
`DIRECT_VM_OMIT_PRIVATE_SUPERVISION_CONTROL=1` omits only the kernel argument;
it changes no assertion. That
[negative control](https://app.buildbuddy.io/invocation/8c097f7b-302a-4fc8-b5ca-6ac9b8b7e6c0)
starts the real Runtime but emits neither private receipt nor parent pass. The
unchanged receipt wait cannot complete; the existing 120s guest bound exits 124
and the live-guest watcher fails the case. Both paths remove their container,
restore the host owner and report no listener cleanup error. This proves an
omitted fixture is rejected; it does not inject a defect into an individual
private invariant. The different behaviors are not a speed comparison or a
reliability-rate sample.

## Next measurements and admission controls

Before promotion, seed launcher/mount, issuer/Pod-binding, kernel-limit and
supervision defects against the new composition; keep the old full gate until
each unique assertion has an owner and its relevant control fails. Then run a
fixed-source, cache-classified comparison including Pod, MCP Services and every
other selected test. A faster isolated VM result cannot remove those other
tails. Track this work in [#16099](https://github.com/agentydragon/ducktape/issues/16099).
Do not expand to the 30-pair qualification cohort without a promising complete
graph screen.
