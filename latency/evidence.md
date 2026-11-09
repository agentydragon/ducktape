# Completed KVM experiment: evidence and artifact index

This follow-up was assembled at 2026-11-09T11:03:17.556787+00:00. It adds completed action-level measurements and cleanup; it does not refresh the historical CI cohort or Mimir telemetry window.

- Configuration/evidence PR: [#15124](https://github.com/agentydragon/ducktape/pull/15124), evidence commit `791540fbaa160bde261e469adfdd2fcc84f378ec`.
- Fixed CPU-test source: `de9e7c656c00a1dc1e758101bf7dd8009c3d57ae`; all six existing full Firecracker target runs passed.
- [Durable measurements and cleanup receipt](https://github.com/agentydragon/ducktape/blob/791540fbaa160bde261e469adfdd2fcc84f378ec/devinfra/buildbuddy/debug/2026_11_09_hil_kvm_worker_canary.json), [interpretation and limitations](https://github.com/agentydragon/ducktape/blob/791540fbaa160bde261e469adfdd2fcc84f378ec/devinfra/buildbuddy/debug/2026_11_09_hil_kvm_worker_canary.md).
- [Pages supporting data](evidence.json): `resource_telemetry_followup.cpu_experiment`; exact UTC command windows, input identity, per-action CPU/memory bounds, phase profiles and I/O pressure.
- Fixture trim [PR #15099](https://github.com/agentydragon/ducktape/pull/15099) verified merged at 2026-11-09T10:21:37Z, merge `7c0745f7527876caeb8002e80c7d4f19996d8beb`. Post-merge complete-CI impact remains unmeasured.

## Native profile archives

Seven archives contain allowlisted sysstat, PSI and owned cgroup/lifecycle diagnostics. Journals, consoles, environment and credential files are excluded. Each upload was downloaded and SHA256/size verified. CAS follows its normal retention policy; compact measurements are committed independently of CAS lifetime.

From the repository Nix devshell:

```sh
bb download HASH/SIZE > profiles.tgz
sha256sum profiles.tgz
wc -c profiles.tgz
```

| Arm            | SHA256                                                             |   Bytes |
| -------------- | ------------------------------------------------------------------ | ------: |
| cpu4-arm1      | `61b464f789de0f6480c13bf516bd99c5171821947f55d305a7217da8587a13ed` | 1182303 |
| cpu4-arm2      | `6949d5019cc3b4e1ed44b64f771ee94280f151b2be768d2a879ad1b7ef088fbd` | 1143479 |
| cpu4-arm3      | `2e6e35fcb24895dbf1418e4efebb05cd4c08e8c01123e6fd26e06c568a793d3e` | 1039319 |
| cpu8-arm1      | `05737af038efef4c360109e2375c5f99e5a7fdb43adc5cbb8addc20f4ce26b3c` | 1457285 |
| cpu8-arm2      | `b0499b98af7145b3c10179c99c270b9da80c699732a51483f846e3180e0a7941` | 1393037 |
| cpu8-arm3      | `77de041ac0374b6d391a49ff28e681508794b9720c1d4fbf7b52241986d6665e` | 1651025 |
| candidate-arm1 | `416eba73d5b431d40358b0387b1ecf7d7e8eef17335f365d839dc0d447e44888` |  802379 |

Use the hash and byte count as `HASH/SIZE`; exact bytestream URIs are retained in the JSON.

## Interpretation and validation boundary

Retain 4 vCPU / 16 GiB. The three cold/warm matched-state pairs were ordered, not randomized, on one shared host in two guest lifetimes. The final 8-CPU run followed an idle-task watchdog warning outside measured command windows; its cause remains unproven. The CPU experiment used the untrimmed source throughout. No matched HDD/SSD or smaller-RAM intervention was completed. These action times do not establish mean full CI below four minutes.

Cleanup verified at 2026-11-09T10:57:53.166378+00:00: all nine owned physical TestRunners passed; counters/lifecycle/idle checks reconciled, executors gracefully unregistered, all three owned objects and guest/scratch resources were removed, physical free space returned to 133.57 GB and production VM/VMI identities remained unchanged and Ready.

The [RBE browser check](https://app.buildbuddy.io/invocation/67b8a12f-b492-4249-90a4-6aaf1e5ebe7e) passed all four desktop/mobile and light/dark cases. It exercised actual CDNs, live preference changes, six CPU bars and exact-value rows, telemetry controls, retained phase bars and span timelines, PNG byte identity and rendered asset hashes. Native artifacts contain 80 screenshots. The new CA section was inspected in all four cases; representative CPU chart/table, first-screen and retained timeline captures were also reviewed. The current `visual-validation.json` records native artifact URIs, hashes and review limits. This follow-up is reviewable through its PR and redeploys after merge to `gh-pages`.

## VM readiness follow-up

The [original failed child](https://app.buildbuddy.io/invocation/46993f63-7c5b-5685-a009-4664f7c8318f) stopped at guest uptime 5.157459 seconds in early PCI/ACPI boot, before systemd/runner. The post-destruction QEMU snapshot cannot establish its cause. Failure-time capture [PR #15142](https://github.com/agentydragon/ducktape/pull/15142) merged at 11:45 UTC. This original stall remains unresolved.

The independent 45-byte virtiofs tag regression was repaired by [PR #15144](https://github.com/agentydragon/ducktape/pull/15144), merged at 11:33 UTC. It documents and enforces the 36-byte UTF-8 limit and completes the Debian guest mount. Controller and guest-layout checks passed. The next uncached VM suite failed earlier, during service PKI bootstrap.

## Fixture CA reuse: reproduced and repaired

The [failed suite](https://app.buildbuddy.io/invocation/e77e85c9-4306-4e40-8593-6b5e0cb3a54c) retained an intermediate whose CertificateRequest was created at 11:16:21 UTC while recreating the root at 11:30:42 UTC. All five service chains verify through the captured old root and fail through the replacement root. The incomplete cleanup removed only the root; the intermediate and its Secret survived in the retained cert-manager namespace. Public certificate fingerprints and source/artifact identities are in `evidence.json → vm_readiness_followup.fixture_pki_followup`. The retained artifact exposes CertificateRequest chains, not independently captured live Secret/target ConfigMap bytes.

The [real two-cycle control](https://app.buildbuddy.io/invocation/b232674e-e454-4d58-b798-50592d74348e) passed its first issuance and cleanup, then failed the second chain verification with `InvalidSignature`. With [PR #15193](https://github.com/agentydragon/ducktape/pull/15193), the [same uncached regression](https://app.buildbuddy.io/invocation/84b1a207-dcb2-4adc-8061-8fd3c73628b7) passed both cycles, changed root/intermediate Certificate identities and retained the same controller Deployment identities. It uses real K3s, cert-manager and trust-manager with the fixture's CA hierarchy, without application workloads. It verifies the repair at that boundary. The 214.999-second control and 90.967-second repair had different cold-start/cache conditions; they are not a latency treatment.

The repair derives fixture-owned retained-namespace Certificates and declared Secrets, deletes UID-owned requests with absence checks, preserves unrelated controller credentials, and includes the cleanup helper in the baseline fingerprint to reject old snapshots. Focused tests and named-library builds passed. Hosted full-suite CI is pending. Current devel trusts the intermediate directly and may mask the original service symptom; per-action identities still require fresh issuance. The latest inspected green VM summary at 17:41 UTC was cached. Fresh full guest boot, the early-kernel stall's cause and mean complete CI below four minutes remain unproven.
