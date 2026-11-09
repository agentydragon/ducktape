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

This Pages update has formatting/data checks only at preparation. Browser review of the updated assets is pending: desktop/mobile, initial light/dark, live preference changes, new six-bar CPU chart and exact-value table, retained telemetry controls and span timelines. The existing `visual-validation.json` describes the preceding assets and is not proof for this revision. The report is reviewable through its PR and is not deployed until merge and Pages deployment.

## VM readiness follow-up

[Failed child](https://app.buildbuddy.io/invocation/46993f63-7c5b-5685-a009-4664f7c8318f) at PR #15126 source `a099f643`: VM2 console and raw serial stop at guest uptime 5.157459 s in early PCI/ACPI boot, before systemd/runner. The default lane used QEMU 10.1 TCG without `/dev/kvm`, 2 guest vCPUs and 1.5 GiB. ConnectionPending at 300 s is a downstream symptom. The QEMU host snapshot at 09:57:08 UTC followed destruction at 09:56:56 UTC; root cause remains unresolved. Failure-time passive capture and [one full-suite observer attempt](https://app.buildbuddy.io/invocation/cb469644-f04f-4844-9021-f3a425dce6ef) were pending at handoff. A separate same-invocation Pod failure was Unschedulable / Insufficient memory. No TLS failure or fix is established.
