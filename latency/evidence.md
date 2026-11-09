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

The final [RBE browser check](https://app.buildbuddy.io/invocation/34d5ec6e-ccda-43a2-8014-b3357736afdc) passed all four desktop/mobile and light/dark cases and captured 96 screenshots. It exercised actual pinned CDNs, live preference changes, the new VM chart and Pod budget table, Mantine startup-history expansion, all retained chart controls, downloads, overflow and rendered hashes. The Pod proof was inspected in all four cases, with representative first screens, VM/CPU charts, mobile tables, expanded history and historical timeline also reviewed. Mobile tables retain Mantine horizontal scrolling; captures show their left edge. Other captures were not all manually inspected. `visual-validation.json` records exact native URIs, hashes and review limits. The rendered HTML, module, JSON and PNG stay unchanged after that check; the final manifest adds publication/validation metadata and the updated unrendered evidence-index hash.

## VM readiness follow-up

The [original failed child](https://app.buildbuddy.io/invocation/46993f63-7c5b-5685-a009-4664f7c8318f) stopped at guest uptime 5.157459 seconds in early PCI/ACPI boot, before systemd/runner. The post-destruction QEMU snapshot cannot establish its cause. Failure-time capture [PR #15142](https://github.com/agentydragon/ducktape/pull/15142) merged at 11:45 UTC. This original stall remains unresolved.

The independent 45-byte virtiofs tag regression was repaired by [PR #15144](https://github.com/agentydragon/ducktape/pull/15144), merged at 11:33 UTC. It documents and enforces the 36-byte UTF-8 limit and completes the Debian guest mount. Controller and guest-layout checks passed. The next uncached VM suite failed earlier, during service PKI bootstrap.

## Fixture CA reuse: reproduced and repaired

The [failed suite](https://app.buildbuddy.io/invocation/e77e85c9-4306-4e40-8593-6b5e0cb3a54c) retained an intermediate whose CertificateRequest was created at 11:16:21 UTC while recreating the root at 11:30:42 UTC. All five service chains verify through the captured old root and fail through the replacement root. The incomplete cleanup removed only the root; the intermediate and its Secret survived in the retained cert-manager namespace. Public certificate fingerprints and source/artifact identities are in `evidence.json → vm_readiness_followup.fixture_pki_followup`. The retained artifact exposes CertificateRequest chains, not independently captured live Secret/target ConfigMap bytes.

The [real two-cycle control](https://app.buildbuddy.io/invocation/b232674e-e454-4d58-b798-50592d74348e) passed its first issuance and cleanup, then failed the second chain verification with `InvalidSignature`. With [PR #15193](https://github.com/agentydragon/ducktape/pull/15193), the [same uncached regression](https://app.buildbuddy.io/invocation/84b1a207-dcb2-4adc-8061-8fd3c73628b7) passed both cycles, changed root/intermediate Certificate identities and retained the same controller Deployment identities. It uses real K3s, cert-manager and trust-manager with the fixture's CA hierarchy, without application workloads. It verifies the repair at that boundary. The 214.999-second control and 90.967-second repair had different cold-start/cache conditions; they are not a latency treatment.

The repair derives fixture-owned retained-namespace Certificates and declared Secrets, deletes UID-owned requests with absence checks, preserves unrelated controller credentials, and includes the cleanup helper in the baseline fingerprint to reject old snapshots. Focused tests and named-library builds passed. Hosted full-suite CI failed Pod memory admission and MCP delegation after bootstrap. Current devel trusts the intermediate directly and may mask the original service symptom; per-action identities still require fresh issuance. Fresh full guest boot subsequently passed as recorded below. The original early-kernel stall's cause and mean complete CI below four minutes remain unproven.

## Fresh VM execution and hardware trial

A [fresh hosted VM execution](https://app.buildbuddy.io/invocation/aed9b899-fa04-5901-8bdd-68f5b8d00b69) passed all three scenarios and cleanup at synthetic source `9758d0a54b141afcf38e1ba727b3fdc629b6a1ee`: 423.143 action seconds, 380.54 pytest seconds. The next PR head reused the same in-flight action, so this is one physical execution. Setup was 127.441 s, case work 216.689 s and cleanup 29.930 s; 49.082 s lay outside those disjoint spans. Concurrent readiness waits were 84.884, 88.266 and 116.220 s.

The [existing KVM target](https://app.buildbuddy.io/invocation/2d631f92-b683-44de-a13b-59a3b7afdae2) then passed the same scenarios and cleanup: 227.127 action seconds, 220.52 pytest seconds and 278.444 s for its Bazel invocation before outer runner admission. All three QEMU snapshots contain `-accel kvm`; readiness waits were 39.979, 41.707 and 45.351 s. Its local source was `fb0ce2bfe8204287752f51e88bcd71c64ea1f235`; only investigation notes differ from the TCG source. `evidence.json → vm_readiness_followup.vm_hardware_trial` records command, remote patch base, native profiles, source comparison and limits. The first command accidentally inherited CI's manual exclusion and was cancelled; the corrected invocation explicitly selected the manual target. Debian mirror 404 warnings were nonterminal and dependency loading recovered.

This one ordered architectural comparison changes worker hardware/placement, estimated CPU budget, isolation, baseline lifecycle and cache history. The observed 196.015-second difference is not an isolated KVM causal effect or a fleet forecast. The KVM action is below four minutes; its invocation and mean complete CI are not. Capacity and queue measurements are required before routing normal VM CI to the existing shared four-CPU KVM pool. Preserve a TCG lane if its distinct emulation coverage is required. No infrastructure or scheduling policy was changed.

Broader [hosted CI](https://app.buildbuddy.io/invocation/bc8b6a85-f8f5-5fd8-93d9-9424c28a74e0) is red: Pod messaging could not admit a memory request and waited 300 s (19/20 cases passed,749.181-second action); MCP delegation lost an exec-server session during an executor liveness restart and then parsed an empty receipt (444.605-second action). These are distinct from the CA mismatch. Accelerating the 423-second VM action would not remove the longer Pod action from this head's critical path. No passing retry is being counted as a repair.

## Pod admission ledger and resource trial

The native Node had 9,724.5625 MiB and 7,000m CPU allocatable. Steady services request 1,734 MiB/640m; each runner requests 1,568 MiB/760m, including its isolated executor and running egress init container. Only five fit. A retained pair plus the four-runner messaging case needs six, so CASE_LIMIT=1 alone is insufficient. Two retained pairs, messaging and another fresh case can request ten: 17,414 MiB/8,240m total. Requests are admission budgets, not actual RSS. Exact native artifact URIs and source limits are in `broader_ci.failures.pod.admission_ledger`.

The first resource attempt emitted four units despite a CLI default override and was excluded. The corrected rule attempt failed an external Go download before tests. The identical [retry](https://app.buildbuddy.io/invocation/8cf5c916-cbfe-4593-8e91-48fad41bbfb1) passed all 20 cases: native Action/Command confirm identical inputs, arguments and environment, with only compute units 4→8. Before/final Node snapshots agree on 19,543.824 MiB / 11 CPU, above the intended 17,414 MiB / 8.24 CPU request envelope. Native Events show no failed scheduling; pool identity checks, four pooled-runner cleanup spans and suite cleanup completed. Retained logs, Events and final Pod states contain no admission/OOM/eviction failure marker; this is not peak-RSS or full kernel-history proof.

Physical action 555.348 s; pytest 532.183 s; input fetch 1.024 s; setup 221.126 s; concurrent case envelope 257.398 s; stack cleanup 38.497 s. Cases overlap and are not additive. The slowest case spans were CA-at-runner-address 101.483 s and explicit-resume 82.761 s. This one cold-baseline trial proves capacity fit and a passing execution; it does not establish a causal speedup, optimal reservation, fleet reliability or complete-CI mean. [PR#15212](https://github.com/agentydragon/ducktape/pull/15212) scopes eight units to test_pod and preserves all 20 cases. Current-head CI is separate. Durable source evidence is in `agentplane/system_tests/k3s/debug/pod_admission.{md,json}`; exact native URI/hash receipts, phase clocks and20case spans are also in `evidence.json → vm_readiness_followup.pod_resource_trial`. The negative Firecracker CPU trial held RAM fixed and had no Pod admission failure; it is not evidence against this admission repair.

CA repair #15193 merged at 2026-11-09T19:04:13Z as `1371e88ac93bcd67d9755138dc3511047f6178c7`. Fresh proof predates merge; the last evidence-only PR CI was cancelled after merge. No deployment proof or complete green CI is claimed. Fresh input fetch was 3.229 s for TCG and 8.226 s for KVM; a local cache proxy cannot remove the dominant measured setup, scenario or admission work.

## MCP executor health-loss boundary

A shared K3s/Kine API and kubelet/containerd responsiveness stall preceded the executor liveness restart and exec-server session loss. Kine operations took 1.79/2.05 s, lease/API calls timed out and kubelet housekeeping took 12.342 s. Policy, Sandbox and trust-manager containers exited 1 during the same window. The post-event sample had 4.77 GB available and zero CPU/memory/I/O PSI; coarse before/final samples cannot exclude a short spike. Old controller stdout, termination reasons and contemporaneous pressure counters are missing, so the underlying stall and exact controller exit paths remain unresolved. Exact timestamps and all 11 independently hash-checked native references are in `broader_ci.failures.mcp.health_loss_followup`. No repair or OOM cause is claimed.

## Next scenario measurements

The CA-at-runner-address case took 101.483 s. After impostor startup, the process trace shows an 83.571 s interval from the first unnamed failed WatchState to an identified successful impostor connection. Source replaces the same mounted Secret after proving actual wrong-issuer handshake rejection; kubelet sync is one minute, Runtime reloads changed certificate files and Gateway retries every second. Mounted credential delivery is the strongest hypothesis, but Secret-write and mounted-file-change timestamps are missing and failed RPCs lack Sandbox attribution. This interval is not measured Secret-projection time. The next candidate is a test-owned Pod refresh trigger after Secret replacement, retaining Pod UID/address, actual rejection, legitimate-credential recovery and the original runner's health. A staging directory would remove Secret-projection coverage and needs a separate decision. A Secret API watch alone cannot prove mounted bytes changed.

Explicit resume took 82.761 s and spent 60.104 s before intentional idle quiescence, matching its configured 60-second threshold. Replacing it with operator suspension would lose the Idle-origin assertion. Measure a shorter test-only idle policy while preserving that assertion if this becomes the next intervention. No candidate has been implemented or tested here. The native process trace was independently SHA256/length-verified: `bytestream://remote.buildbuddy.io/blobs/53fac002f4bc61a1d141bb343df07627d7bacb687c1f19bd19213efff2194696/32754450`.

The excluded CLI-default resource attempt settled with native `TESTS_FAILED` after its cancellation request was accepted. Its Action still requested four units; it is not an eight-unit measurement, regardless of that final state.

Reusable workflow candidates for the skills: verify emitted Action/Command properties and input identity before waiting for a resource experiment; collect a capacity/request-budget/result receipt; retain a shared four-case RBE report harness with chart, theme, overflow, download and asset-hash checks. Native `bb download --type=Action` and `--type=Command` replace a custom protobuf decoder.
