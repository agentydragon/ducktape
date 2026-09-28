# Cilium agent fatals on kernel ≥ 7.2-rc1: `bpf_set_retval` feature probe

**Date**: 2026-07-16. **Status: Resolved 2026-09-28.** Cilium 1.19.8 is
deployed, and `cilium-agent` is Ready on `rugged` running Linux 7.2.7. The upstream
Cilium connectivity-check manifest reported all 14 deployments Available with
workloads on `rugged` and a Talos peer. After `rugged` woke, Cilium health confirmed
host and endpoint ICMP/HTTP reachability; cluster health was 9/10 because `iguana`
remains offline. The Cilium-derived kernel ceiling below 7.2 is retired.
`ipu7-camera.nix` now uses `linuxPackages_latest`; in the current flake lock, that
alias resolves to the exact Linux 7.2.7 derivation that passed validation. The
hardware minimums remain 6.17 for IPU7 and 7.1.8 for the Xe/TTM fix.

This record keeps the dated incident chronology and the temporary kernel mitigation
for future reference. The release, rollout, and Linux 7.2 validation were tracked in
[ducktape#6825](https://github.com/agentydragon/ducktape/issues/6825).

**Historical chronology.** The 2026-08-26 remediation first used
`linuxPackages_latest`, which then resolved to 7.1.8. A flake update moved the alias
to 7.2 and the next rebuild exposed the Cilium crash. A 7.1-series pin resolved the
immediate failure on 2026-09-05. PR #7078 then deliberately moved `rugged` to the 7.2
series on 2026-09-16 before the Cilium fix shipped, accepting temporary CNI loss on
this roaming node.

At that time, the kernel needed a floor (≥ 6.17 for IPU7) and a temporary Cilium
ceiling (< 7.2). Cilium 1.19.8 removes that ceiling; the hardware minimums remain,
and the repo now follows the locked `linuxPackages_latest` alias.

As of 2026-09-13, the stable Cilium backport was merged but not yet in the latest
release (v1.19.7). The v1.19 backport is [Cilium PR #48376](https://github.com/cilium/cilium/pull/48376);
it changes `HAVE_SET_RETVAL` detection to use `bpf_core_enum_value_exists()`. Cilium
1.19.6 was observed fatalling on kernel 7.2.0 on 2026-09-04. The fix later shipped
in v1.19.8 and was selected in [ducktape PR #8382](https://github.com/agentydragon/ducktape/pull/8382).

## Symptom

`cilium-agent` on `rugged` (NixOS, `linuxPackages_testing` = 7.2.0-rc2) crash-loops at
startup (36 restarts observed):

```text
level=fatal msg="failed to probe helper" progType=CGroupSock helper=FnSetRetval
  error="detect support for FnSetRetval for program type CGroupSock: load program:
  invalid argument: 0: (85) call bpf_set_retval#187: R1 is not a scalar"
```

With no CNI agent, every new pod sandbox on the node fails
(`FailedCreatePodSandBox: unable to connect to Cilium agent`), taking out the
node-pinned DaemonSets (promtail, node-feature-discovery) and
`egress-proxy-rugged`.

## Root cause

Kernel commit `b1f7f67b74c2` ("bpf: Add validation for bpf*set_retval argument",
2026-06-05, first in 7.2-rc1) hardens the verifier: `bpf_set_retval()`'s argument
was `ARG_ANYTHING`, and a \_positive* retval could bypass `err < 0` checks in four
cgroup-hook paths (NULL/wild-pointer derefs). The verifier now requires R1 at the
call site to be a **known scalar within `[-MAX_ERRNO, 0]`** (LSM hooks: the hook's
retval range). Intentional hardening; a revert upstream is unlikely.

The breakage is in **feature probing**, not the datapath:

- `cilium/ebpf` `features.HaveProgramHelper` (`features/prog.go`) probes helper
  support by loading `call <helper>; mov r0, 0; exit` — with R1 still holding the
  program's _context pointer_ at entry.
- Old kernels: accepted (`ARG_ANYTHING`), or EACCES for badly-set-up args, which
  the prober maps to "supported".
- Kernel ≥ 7.2-rc1: **EINVAL** with verifier log `R1 is not a scalar`, which
  matches neither of the prober's "unsupported" patterns (`invalid func`,
  `unknown func`) → the probe returns a raw error.
- Cilium's `pkg/datapath/linux/probes.HaveProgramHelper` does `logging.Fatal` on
  any probe error that isn't `ErrNotSupported` → agent dies before doing anything.

## Blast radius

Every released Cilium (probe unchanged in `cilium/ebpf@main` as of 2026-07-16) on
every kernel ≥ 7.2-rc1. Not specific to our config. No upstream report existed as
of 2026-07-16 (searched cilium/ebpf and cilium/cilium for `set_retval` /
`R1 is not a scalar`).

## Cascade in our cluster (2026-07-13 → 07-16)

`rugged` CNI-down plus `iguana` NotReady left the descheduler's
`LowNodeUtilization` evicting ~8 pods from `ovh-ns103711` every 15 min into a
no-fit reschedule loop (Multi-Attach volume errors, containerd
`failed to reserve container name` races, readiness churn). Fixed independently:
descheduler `nodeFit` (#3276), stuck-Job GC (#3279). Unrelated same-window noise:
`forgejo-images-creds` truncation (#3280), kyverno haku-state audit spam (#3282).

## Resolution and historical kernel mitigations

Cilium 1.19.8 contains the v1.19 FnSetRetval probe fix from
[Cilium PR #48376](https://github.com/cilium/cilium/pull/48376). Ducktape PR #8382
updated the chart; after rollout, the connectivity-check suite passed on Linux 7.2.7
on `rugged`. No Cilium-specific kernel workaround remains.

The following local options record decisions made before the upstream fix shipped:

- **Kernel patch revert considered**: revert `b1f7f67b74c2` in the testing kernel.
  This would preserve the Xe experiment baseline, but was not used as the final fix.
- **7.1 series pin applied**: `linuxPackages_7_1` carried the Xe/TTM fix while
  avoiding the Cilium failure. PR #7078 later moved the host to `linuxPackages_7_2`
  ahead of the Cilium release; the 1.19.8 rollout and Linux 7.2.7 validation resolved
  the resulting incompatibility.

## References

- Kernel: `b1f7f67b74c2` (verifier `BPF_FUNC_set_retval` case, error at
  `kernel/bpf/verifier.c` "R1 is not a scalar"); selftests `7913cdb54ee3`,
  `6fa2839893e3`.
- Prober: `cilium/ebpf` `features/prog.go` `haveProgramHelper`; fatal wrapper
  `cilium/cilium` `pkg/datapath/linux/probes/probes.go` `HaveProgramHelper`
  (probe list `{ebpf.CGroupSock, asm.FnSetRetval}` → `HAVE_SET_RETVAL` in
  `bpf/include/bpf/features.h`).
