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

## Next measurements and admission controls

Use the existing boot/launcher/Runtime diagnostics to explain readiness before
expanding expensive comparisons. In this run the readiness span begins at
00:11:13.376Z, the launcher reports domain start at 00:11:46.252Z, the console
reports the private supervision pass at 00:12:55.644Z, and readiness returns at
00:13:05.830Z. These are cross-process observations, not an exact additive
breakdown of kernel or controller CPU. Establish where the roughly 33s before
domain start and 69s until the supervision receipt are spent.

Before promotion, seed launcher/mount, issuer/Pod-binding, kernel-limit and
supervision defects against the new composition; keep the old full gate until
each unique assertion has an owner and its relevant control fails. Then run a
fixed-source, cache-classified comparison including Pod, MCP Services and every
other selected test. A faster isolated VM result cannot remove those other
tails. Track this work in [#16099](https://github.com/agentydragon/ducktape/issues/16099).
Do not expand to the 30-pair qualification cohort without a promising complete
graph screen.
