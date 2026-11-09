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
No fresh guest-boot result or cause for the original early-kernel stop is claimed.
The cleanup helper now participates in the baseline fingerprint, so the first
attachment also rejects old snapshots certified by the incomplete cleanup.
