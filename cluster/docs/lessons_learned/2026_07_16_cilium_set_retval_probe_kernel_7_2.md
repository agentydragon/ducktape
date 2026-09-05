# Cilium agent fatals on kernel ≥ 7.2-rc1: `bpf_set_retval` feature probe

**Date**: 2026-07-16. **Status**: resolved for `rugged` 2026-09-05 by pinning the
7.1 _series_ (`linuxPackages_7_1`) in
<../../../nix/nixos/hosts/rugged/ipu7-camera.nix>, where the bounds that produce
it are recorded.

**A floating alias is not a pin.** The 2026-08-26 remediation set
`linuxPackages_latest`, correct at the time because the alias then resolved to
7.1.8. A flake update moved it to 7.2, and the next rebuild put `rugged` back on
the broken kernel — `cilium-agent` crash-looped, taking the node's CNI and its
DaemonSets with it. What this host needs is a floor (≥ 6.17, IPU7 camera) and a
ceiling (< 7.2, this bug); an alias encodes neither and tracks whatever upstream
ships. A series attribute encodes both and cannot cross the ceiling.

Upstream `cilium/ebpf` probe fix (remediation 1) is still not shipped: Cilium
1.19.6 was observed fatalling on kernel 7.2.0 on 2026-09-04. The general "every
kernel ≥ 7.2-rc1, every released Cilium" blast radius remains open for anyone
actually needing a ≥ 7.2 kernel.

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

## Fix paths

1. **Upstream `cilium/ebpf`** (real fix): in `features/prog.go
haveProgramHelper`, special-case `FnSetRetval` to prepend
   `asm.Mov.Imm(asm.R1, 0)` — a constant 0 is valid on both old (`ARG_ANYTHING`)
   and new (in-range known scalar) kernels, so the probe loads cleanly with no
   log-sniffing. Cilium then needs a vendored bump + patch releases.
2. **Kernel list heads-up** (time-sensitive, optional): 7.2 is at rc2;
   `bpf@vger.kernel.org` should know the hardening bricks startup of every
   released Cilium ("breaks userspace" datapoint) while there is still time
   before final.
3. **Local remediation for `rugged`** — constrained by the deliberate
   `linuxPackages_testing` pin for the Xe/TTM swap-storm A/B test
   (<../../../nix/nixos/hosts/rugged/default.nix>, needs `ba7fd1634228`):
   - **A (preferred)**: `boot.kernelPatches` revert of `b1f7f67b74c2` on the
     testing kernel. Keeps the Xe experiment bit-exact; the dropped hardening is
     irrelevant on a personal laptop node. Remove once a Cilium release ships the
     fixed prober.
   - **B (applied)**: a released kernel carrying `ba7fd1634228`. Cilium-compatible,
     but changes the Xe experiment's baseline. Name the **series**
     (`linuxPackages_7_1`), never `linuxPackages_latest`: the alias satisfied this
     when it resolved to 7.1.8 and then floated to 7.2, which is how the bug
     returned on 2026-09-04.

## References

- Kernel: `b1f7f67b74c2` (verifier `BPF_FUNC_set_retval` case, error at
  `kernel/bpf/verifier.c` "R1 is not a scalar"); selftests `7913cdb54ee3`,
  `6fa2839893e3`.
- Prober: `cilium/ebpf` `features/prog.go` `haveProgramHelper`; fatal wrapper
  `cilium/cilium` `pkg/datapath/linux/probes/probes.go` `HaveProgramHelper`
  (probe list `{ebpf.CGroupSock, asm.FnSetRetval}` → `HAVE_SET_RETVAL` in
  `bpf/include/bpf/features.h`).
