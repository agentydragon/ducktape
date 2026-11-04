# K3s inside a BuildBuddy Docker action

The manual `//agentplane/system_tests/suites:test_vm_kvm` target uses the existing
`wyrm2-kvm-vm` worker. Its Docker action starts K3s and containerd, which start the
supporting Pods before KubeVirt can boot the three scenario guests. Passing the
`/dev/kvm` smoke check does not establish that these nested containers can start.

The [updated-worker run](https://app.buildbuddy.io/invocation/fa0471c2-a9d1-4d0a-a3c7-a1c2d73db333),
at source `22771892d0251c0432fb94889857a506c8d52751`, failed before any scenario.
Nested runc reported `unable to create session key: operation not permitted` for
CoreDNS, local-path, cert-manager and KubeVirt support Pods. The PKI rollout then
exhausted its existing 180-second deadline. The 376.30-second test action followed
324.03 seconds of scheduler wait; neither is a guest boot measurement.

A [direct syscall probe](https://app.buildbuddy.io/invocation/ef144a2d-ffc7-47a8-86c5-a98be3b2a7e8)
had already reproduced `KEYCTL_JOIN_SESSION_KEYRING` returning `EPERM`, while KVM
API version 12, VM creation and vCPU creation succeeded. The updated worker still
used Docker's built-in seccomp profile with no executor `SecurityOpt` override.
[Docker documents](https://docs.docker.com/engine/security/seccomp/) that its
default profile denies `keyctl`.

Containerd's standard `NoNewKeyring` option lets the trusted fixture containers
inherit the action's session keyring. The
[option schema](https://github.com/containerd/containerd/blob/v2.1.5/api/types/runc/options/oci.proto)
and [runc integration](https://github.com/containerd/containerd/blob/v2.1.5/cmd/containerd-shim-runc-v2/runc/container.go)
define this behavior. The candidate enables it only for the manual KVM fixture;
the worker's Docker policy and guest VM kernels stay unchanged.

The [candidate run](https://app.buildbuddy.io/invocation/a423660b-4e4c-44ba-9a22-8676498a194f)
uses runtime commit `5d4422d221`. Its lint and type checks passed. Full-suite
acceptance remains pending: verify containerd's startup configuration, all three
scenarios, actual KVM acceleration, boot/shutdown evidence, trace checks and
cleanup before calling the lane usable.
