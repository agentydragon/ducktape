# Fixture workload CA survives its root

In [VM suite e77e85c9](https://app.buildbuddy.io/invocation/e77e85c9-4306-4e40-8593-6b5e0cb3a54c), source
`d2e6f15c4af683a87a5b7dfc10c7d3758d1c2aa6`, stack setup failed before any
VM scenario. Gateway local-chain verification and service probes rejected the
certificate chain against `test-k3s-runner-root`.

The retained CertificateRequests establish the mismatch:

| Public certificate                                              | Request creation | SHA256                                                              |
| --------------------------------------------------------------- | ---------------- | ------------------------------------------------------------------- |
| Prior test root, retained in the workload request's `status.ca` | 11:16:21 UTC     | `f63e5afb2df4f81160482b91e1c3bfded24dbeddfdd851ae1e7b02ed8c95c003`  |
| Workload intermediate                                           | 11:16:21 UTC     | `8960ce33134179cf29ccd076bc6696c31b4b4ae93aa0f1944f59c3552b93e80d`  |
| New test root                                                   | 11:30:42 UTC     | `6ba04223b258ee9b8628c8911f7c30771ccd610859fa46669fb0c15f479c8dea0` |

All five failing service chains contain that same intermediate. OpenSSL accepts
each through the captured old root or with the intermediate as a partial-chain
trust anchor; each fails through the new test root. Both roots have the same
subject, `CN=test-k3s-runner-root`, and different keys. Ready Certificates and
Synced Bundles therefore did not establish a coherent current chain. The artifact
retains CertificateRequest certificate status and Bundle source references, not
the live Secret or target ConfigMap bytes; those stored bytes were not compared
independently.

`stack._baseline_clean` deleted only the hardcoded root Certificate, its owned
requests, and its Secret. The workload CA also belongs to the per-action fixture,
but lives in the retained `cert-manager` namespace, so namespace deletion left it
and its Secret alive. The next fixture recreated the root but reused the old
intermediate. Cleanup must derive all such certificates from the rendered
fixture, remove their owned requests and named Secrets, and verify absence before
certifying reuse. Retained controller credentials are outside that ownership set.

Current devel additionally changed server SVID verification to trust the workload
CA directly (`cd01bf992c`). That can avoid the old root-chain symptom; it does not
make reuse of per-action fixture identities correct. The two-cycle regression
checks fresh root/intermediate identities and valid chains while retaining the
real cert-manager/trust-manager Deployment identities. It uses the production-
derived fixture CA objects without application workloads.

This is separate from the original early-kernel VM console stop. A coherent PKI
bootstrap does not identify that stall's cause.

The [uncached control](https://app.buildbuddy.io/invocation/b232674e-e454-4d58-b798-50592d74348e)
passed its first issuance and cleanup, then failed with `InvalidSignature` on the
second issuance. With the cleanup repair, the [same uncached test](https://app.buildbuddy.io/invocation/84b1a207-dcb2-4adc-8061-8fd3c73628b7)
passed both cycles: root and intermediate Certificate UIDs changed, chains
verified, cleanup completed twice, and retained controller Deployment UIDs stayed
unchanged. The native public lifecycle artifact, source hashes and clocks are in
[`fixture_ca_reuse.json`](fixture_ca_reuse.json). Control and repair action times
were 214.999 and 90.967 seconds; differing cold-start/cache conditions prevent a
latency treatment claim.

Focused cleanup/stack tests and named-library builds passed. Hosted full-suite
validation is tracked by [PR #15193](https://github.com/agentydragon/ducktape/pull/15193).
The [full VM action](https://app.buildbuddy.io/invocation/aed9b899-fa04-5901-8bdd-68f5b8d00b69)
subsequently passed all three scenarios and cleanup in a fresh physical execution
at source `9758d0a54b141afcf38e1ba727b3fdc629b6a1ee`: 423.143 action seconds,
380.54 pytest seconds. The next head reused that same in-flight execution; it is
one physical run. This verifies guest boot on the repaired branch, not a repair
for the original intermittent early-kernel stop.

Broader CI failed two other cases after bootstrap: Pod messaging could not admit
a memory request and waited 300 seconds; MCP delegation lost its exec-server
session during an executor liveness restart, then reported an empty JSON receipt.
Those do not reproduce the CA-chain mismatch. Their underlying causes and repairs
are separate work; full CI is not green.
The cleanup helper now participates in the baseline fingerprint, so the first
attachment also rejects old snapshots certified by the incomplete cleanup.

## MCP Sandbox startup failures on November 9

The [devel invocation `1e6dc3d2`](https://app.buildbuddy.io/invocation/1e6dc3d2-1d13-565b-b32f-0c399df96474)
tested `b55239863dd62a89d7fdb00bb9571a58db8706eb`. Three independent test
actions, `mcp:test_delegation`, `mcp:test_parent_delegation`, and
`mcp:test_services`, timed out waiting for Sandboxes to become Ready. Each action
retained its own `test.outputs/certificates/resources.yaml`. In each, the
`agentplane-workload-ca-1` CertificateRequest predates `test-runner-root-1` by
about 16 minutes. The request's recorded signing root differs from the new root:

| Test action              | Workload CA request | New root request | Old root SHA256                                                    | New root SHA256                                                    |
| ------------------------ | ------------------- | ---------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------ |
| `test_delegation`        | 17:54:37 UTC        | 18:10:31 UTC     | `c25ab543a4941004aed2c66fbd7e3f809afc77f336bbc1f87391e671ad0549ce` | `93742571db266482fb02a5e96e1d00f52d8946abe8c7ea73c8022d6d3834a643` |
| `test_parent_delegation` | 17:54:28 UTC        | 18:10:39 UTC     | `0f918e3dca675919bb96d5312d36b6de2816cdae3aad912ea3cd66b4a0f7f350` | `4c6748ec0e031d0101c0954e699a19b903abbd7f42c42a302c87e9873b338d86` |
| `test_services`          | 17:54:37 UTC        | 18:10:39 UTC     | `cf4deac7f65986569fdee7f5665cd62d9c80ba6dac32de65b82fecbbcf1d80eb` | `008639775ecdd4fa348d0e34d53f054fc0e4aa4800bac2023a0a00084812373c` |

These SHA256 values are X.509 certificate fingerprints, not hashes of the PEM
files. For each action, `openssl verify` accepts the recorded workload CA
certificate against its old signing root and rejects it against the new root
(`error 20: unable to get local issuer certificate`). Thus the recorded fixture
chain is incoherent in all three actions.

`test_services` also retained two central Sandbox Pod snapshots and their
`environment-runtime.log` files. Pods `systest-central-8f84786e` (UID
`ed8ec59b-8fc7-4606-b06f-e100ecc4a979`) and `systest-central-a66b0b13`
(UID `7c8fa11e-025d-4703-a890-6db2ba76ae52`) each show five runtime restarts,
last exit code 1, and `CrashLoopBackOff`. Both runtime logs end at startup with
`Runner control certificate: verify local certificate chain`, followed by an
unknown-authority error and ECDSA verification failure against
`test-k3s-runner-root`. The first observed failure is local certificate
verification during Runtime construction, before a Runner connection could
establish Sandbox readiness. The retained logs prove this for those two UIDs.
The timed-out delegation and policy Sandbox UIDs are different, and their
previous-container stderr was not retained; their identical recorded CA
mismatch supports the same cause but does not prove each Pod's exit reason.

The tested commit did not contain [#15193](https://github.com/agentydragon/ducktape/pull/15193):
`git merge-base --is-ancestor 1371e88ac93bcd67d9755138dc3511047f6178c7 b55239863dd62a89d7fdb00bb9571a58db8706eb`
exits 1. That cleanup merged at 19:04:13 UTC, after this invocation began;
`git show b5523986:agentplane/system_tests/deployment/stack.py` confirms the
tested `_baseline_clean` still removed only the hardcoded root Certificate.
Current devel's fixture-derived cleanup removes both fixture CAs, their owned
requests, and their Secrets. The comparison source includes the cleanup:
`git merge-base --is-ancestor 1371e88ac93bcd67d9755138dc3511047f6178c7 9189044f28c05244ce64daabaa5ab9d8ff05ec95`
exits 0. After both #15193 and the diagnostic change in #15810 merged,
all three targets passed in a fresh, uncached remote execution at
[invocation `0514b26f`](https://app.buildbuddy.io/invocation/0514b26f-a4b0-5abf-81ba-076ceea0c132)
on source `9189044f28c05244ce64daabaa5ab9d8ff05ec95`. That is one selected
post-fix comparison, not a recurrence-rate estimate. If a later readiness
failure appears, compare its captured current and previous Runtime logs and
Pod UID with the public certificate identities before assigning it this cause.
