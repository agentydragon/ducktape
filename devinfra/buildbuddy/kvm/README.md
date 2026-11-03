# BuildBuddy KVM executor smoke test

This manual test checks that the selected executor can open `/dev/kvm`, reports
KVM API version 12, and creates a VM and vCPU. It logs `CapEff` from
`/proc/self/status` for executor diagnostics. It does not run guest code or
verify Firecracker.

From the repository root, run it after the `wyrm2-kvm` executor is deployed:

```bash
bbr test //devinfra/buildbuddy/kvm:kvm_test --nocache_test_results
```
