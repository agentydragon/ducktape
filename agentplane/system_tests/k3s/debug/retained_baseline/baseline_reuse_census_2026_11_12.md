# Natural backend baseline exposure

The latest twelve green, single-attempt production-backend PR heads in the
published latency cohort were selected in first-workflow order. Their workflow
window is 2026-11-12 22:27:48–23:54:04 UTC. This is a bounded selected sample,
not a fleet hit rate or the failure population. The original report cohort and
its denominators are unchanged. See the adjacent JSON for exact heads, native
invocations, actual tested commits, physical attempts, fingerprints and fresh identities.

Of 36 target opportunities, 18 were explicitly absent from available invocations,
three belonged to one unavailable invocation, three were cached and excluded,
and twelve had fresh outcomes with returned
physical TestRunner records and their own persistence artifacts:

| Suite       | Cold | Warm | Fresh outcomes |
| ----------- | ---: | ---: | -------------: |
| VM          |    2 |    1 |              3 |
| Pod         |    1 |    2 |              3 |
| MCP Actions |    3 |    3 |              6 |

All twelve recorded cleanup complete. The three mutable UIDs (namespace,
PostgreSQL Pod and root Secret) are distinct across these outcomes. Multiple
physical attempts on one target are retained in the JSON; twelve outcomes is
not twelve attempts. A cached persistence artifact describes its original
execution and cannot contribute another warm or cold observation.

The recorded fingerprints form two VM and two non-VM families. A cold result
also occurred on a fingerprint observed warm elsewhere. That does not prove
an avoidable miss: worker/snapshot assignment differs. Existing final artifacts
and normal logs omit the actual rebuild decision, so absent snapshot, dirty
state, incompatible inputs, unhealthy daemon and identity change cannot be
separated retrospectively. No cache policy or retained-state fix is justified
by this sample alone.

`system_test.k3s.baseline.open` now records bounded outcome/reason attributes
and current/previous fingerprint plus previous marker state. Outcome is emitted
only after successful open; a failed open has a reason and span error, but no
completed cold/warm outcome. Reasons distinguish
missing marker, dirty state, fingerprint mismatch, unhealthy daemon, changed
identity and successful reuse. These are existing OTel spans, without ad hoc
timing, credentials, manifest contents or changes to attach/reset decisions.
Changing this compatibility-owning helper itself invalidates the old runtime
fingerprint once, as existing safety requires.

After this lands, examine seven complete UTC days of natural production-backend
CI, or the first 50 fresh retained-suite outcomes if later. Exclude cache hits,
deduplicate physical execution IDs, retain failed/cancelled attempts separately,
and group by backend, worker, recycling key, runtime fingerprint and reason.
Record missing-artifact exposure. A repeated same-worker clean fingerprint
mismatch identifies a component-identity investigation; dirty/unhealthy results
identify cleanup/lifecycle work; missing markers with stable keys require hosted
snapshot-admission evidence. Do that before retaining databases, credentials or
whole mutable app stacks. Tracking: #16027; no measured CI saving is claimed.
