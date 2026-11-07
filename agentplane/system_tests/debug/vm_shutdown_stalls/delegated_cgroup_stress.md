# Delegated cgroup path stress

The opt-in [`delegated_cgroup_stress` guest probe](../../vm/guest/delegated_cgroup_stress)
creates an `io` controller transition inside a systemd-delegated disposable service subtree.
It is a reduced kernel-path diagnostic; it does not reproduce PID 1 changing its own/root
cgroup or prove that cgroup updates caused the original shutdown stall.

## First one-guest smoke: fixture did not exercise the path

The first uncached one-guest run, [BuildBuddy invocation
34cdf126-7bac-489c-a044-31c5b4faf8e1](https://app.buildbuddy.io/invocation/34cdf126-7bac-489c-a044-31c5b4faf8e1),
used source head `324646d2bfd5941cdfa2d73b299f1600bc41cd65` on base
`32f7b63bf0604968e887969a48a45386e7cb4bb9`. The guest reached `multi-user.target`, then the
probe emitted `UNEXERCISED reason=delegate_subgroup_did_not_contain_only_writer`. Its cleanup
marker reported owned children removed, resident stopped, trace restored, and the service
controller restored. The Python test raised `RuntimeError: delegated cgroup stress did not
prove the requested path`; it sent no ACPI request, so this was a fixture failure before any
shutdown measurement.

The failing check counted `cgroup.procs` with an external `wc` while the probe itself was in
the delegated subgroup. That child process can also appear in the file being counted. The
check now uses a sourced BusyBox shell helper that reads and validates the sole PID using
shell builtins. A focused test covers the expected PID, an additional PID, a wrong PID, and an
empty file. A repeated one-guest smoke is required to determine whether the fixture now reaches
the requested kernel path; no result is recorded here yet.
