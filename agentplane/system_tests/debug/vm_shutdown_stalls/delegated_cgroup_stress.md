# Delegated cgroup path stress

The opt-in [`delegated_cgroup_stress` guest probe](../../vm/guest/delegated_cgroup_stress)
creates an `io` controller transition inside a systemd-delegated disposable service subtree.
It is a reduced kernel-path diagnostic; it does not reproduce PID 1 changing its own/root
cgroup or prove that cgroup updates caused the original shutdown stall.

## First one-guest smoke: fixture did not exercise the path

The uncached one-guest run [34cdf126-7bac-489c-a044-31c5b4faf8e1](https://app.buildbuddy.io/invocation/34cdf126-7bac-489c-a044-31c5b4faf8e1)
used source head `324646d2bfd5941cdfa2d73b299f1600bc41cd65` on base
`32f7b63bf0604968e887969a48a45386e7cb4bb9`. The guest reached `multi-user.target`, then the
probe emitted `UNEXERCISED reason=delegate_subgroup_did_not_contain_only_writer`. Its cleanup
marker reported owned children removed, resident stopped, trace restored, and the service
controller restored. The Python test raised `RuntimeError: delegated cgroup stress did not
prove the requested path`; it sent no ACPI request, so this was a fixture failure before any
shutdown measurement.

The failing check counted `cgroup.procs` with an external `wc` while the probe itself was in
the delegated subgroup. That child process can also appear in the file being counted. A
[shell-builtin membership check](https://github.com/agentydragon/ducktape/commit/7637594cb711de46745ca9fdeb6656f8eb26ecef)
was prepared and tested separately, but is not included in merged PR #14811. The second
smoke failed at the same check, so the external child alone does not explain the observation.

## Second one-guest smoke: still unexercised

The [follow-up invocation](https://app.buildbuddy.io/invocation/2264ac32-7bc3-498d-899f-c13664e2f6f3)
checked out base `32f7b63bf0604968e887969a48a45386e7cb4bb9` and applied a patch from the
corrected source branch. Its uncached remote execution selected
`test_ci_guest_shutdown_via_standalone_kubevirt_vmi`, with one cycle, the boot RCU observer,
and the delegated probe enabled. It failed after the guest emitted the same
`UNEXERCISED reason=delegate_subgroup_did_not_contain_only_writer` marker. The cleanup marker
again reported owned children removed, resident stopped, trace restored, and service controller
restored. The observer recorded 62 samples and exited cleanly during teardown.

There is no marker proving the requested function path or any of the 20 transition pairs; the
membership check runs before both. The artifacts do not record the actual subgroup membership,
so they do not establish why the corrected check failed. The shutdown diagnostic contains no
ACPI request or ordered shutdown proof: this is another probe setup failure, not a reproduced
shutdown stall. No conclusion about the original stall's cause or frequency follows from it.
