# Ordinary backend contract graph

The next candidate replaces repeated deployment acceptance for implementation
changes whose behavior has a real process or packaged guest owner. It is not
qualified for adoption. `ci_contracts.json` is in `shadow` mode: ordinary CI keeps
all affected deployed tests and archives the proposed graph with its source
provenance. The four replacement contracts run as ordinary tests, not manual
experiments.

The first coverage domain is the exact source ownership of the Runtime journal.
Bazel resolves its actual `srcs`; a directory name does not grant eligibility. Every changed
file must belong to the declared sources, and every direct rule consuming those
files must be an accounted implementation owner. Mixed changes, BUILD files,
protocols, auth, packaging, manifests and unknown owners keep broad acceptance.
Policy changes themselves force the full sweep. This first domain does not finish
the requested routine backend graph: History, Gateway, MCP and App ownership still
need their own assertion mapping and representative selection evidence.

For eligible changes the candidate replaces the Pod, VM, Services and Firecracker
targets with real Runtime recovery/admission and packaged Claude/Codex Session
contracts. A source audit supports the VM substitution: both direct guests use
the current kernel/root/Runtime package, systemd, real pinned harnesses, Gateway,
Dispatcher and History, including shell write/read, setup/fencing, cgroups,
platform CLI routes, credential revocation and retained ordered History. Native
contracts add restart, crash, reply loss and History unavailability. Deployed VM
ownership remains actual controllers, cert-manager, per-Pod credentials,
KubeVirt and PVC/network wiring. The other three substitutions require the same
assertion-by-assertion audit. That audit rejects extending the current substitution
to whole harness, stream or Session/lifecycle libraries. The source audit identified three seams needing dedicated witnesses: a harness
model call through the real Egress proxy; a real Gateway roll during a live
central read/turn; and Firecracker process continuity across checkpoint/thaw.
Both packaged guest harnesses now send their actual model traffic through the
real sidecar/Egress route to an mTLS scripted endpoint. The tests require an
allowed CONNECT decision for that Sandbox Identity. The native Gateway-roll
contract is still being qualified. Keep the matching deployed cases for the
remaining surfaces; Firecracker checkpoint coverage requires the actual platform. This scoped starting policy
does not narrow the overall goal to journal-only PRs.

The CI driver now submits a separate test-label file while retaining every
affected build target. Materializing test-suite leaves explicitly filters manual
and live API tags. A replacement must resolve to an ordinary runnable test;
missing/manual replacements fail instead of producing a coverage gap. Eligible
required contracts cannot succeed via Bazel's no-tests exit code. Shadow records
contain eligibility, accounted owners, replaced labels and the full candidate
test set alongside the existing merge/base/head identity. The archive also records
the actual submitted leaf labels after the CI tag filter, independently of the
pre-filter selection and shadow proposal.

Next, validate ownership queries and selected labels against the actual Bazel
graph, including protocol/platform counterexamples. Run every selected candidate
test alongside all unaffected required tests, with matched baseline inputs.
Qualify complete required-check feedback, all failures/reruns, p90, flake rate,
CPU, memory, disk, cleanup and cold/busy/burst behavior. The first complete screen
must establish a credible path to 30% before expanding the cohort. Until that
evidence exists, `required` mode is an experimental execution option, not the
ordinary acceptance policy.

The added ownership queries and contract processes have recurring cost. Removing
duplicated deployment work is the expected compensation, still unmeasured. There
is no persistent infrastructure or idle reservation in this design. Maintenance
consists of reviewing explicit ownership when contracts move, maintaining seeded
defect controls and defaulting unknown source consumers to broad acceptance.

## First source/query checks

At `7efbf06932c0b90a3c8bb610e79bbfaeb74e34db` plus overlay artifact
`8ee594d08767c146a83ae5cdb058f44dcd86967ef79eaf3a4bfe8947ea722d5d`, a real
Bazel reverse-dependency query for `journal.go` returns 45 test labels. The
candidate removes exactly the four declared deployment targets, retains 41
labels before CI tags, and materializes 40 runnable candidates. The input 45
already includes the four promoted contracts and one live API test. An honest
control therefore has 40 runnable labels after excluding those five; comparing
against the larger shadow graph would inflate the baseline.

The same selector rejects stipulated Session, protocol, BUILD and mixed
journal/protocol source changes and leaves its input test set unchanged. These
are real ownership/tag queries and classifier counterexamples, not a replay of
actual PR hashing or a timed test execution. Shadow mode changes none of the
ordinary submitted tests. The first query probe rejected every replacement
because of regex backslash interpretation; the corrected boundary pattern passes
the real query instead of relying only on the query test double.

The focused CI-driver checks pass in
[f973ff88](https://app.buildbuddy.io/invocation/f973ff88-892c-4354-8ed7-734f2d18ceaa):
40 JUnit cases with zero failures, errors or skips. These include no ordinary
affected targets with a required contract and rejection of a no-tests exit from
that required contract; the archive verifies actual submitted labels separately
from pre-filter labels.
The strengthened recovery and admission contracts pass in
[254e237c](https://app.buildbuddy.io/invocation/254e237c-d03e-4931-88e6-66fdcf5f0970),
including a third input after History recovery and an unchanged retained prefix.
Both invocations use working-tree overlays on the source above. They are focused
validation, not whole-PR latency, p90 or flake-rate evidence.

The seeded discarded-journal run
[6094b0b9](https://app.buildbuddy.io/invocation/6094b0b9-592f-4152-8244-f6a418b24e8c)
fails at the intended retained-Session assertion after real Runtime restart. It
does not fail during fixture setup. The healthy counterpart above retains that
Session and accepts the next input. This establishes sensitivity to the missing
journal in this arrangement; it is not a measured production flake rate.

The real-model Egress path passes for
[Claude](https://app.buildbuddy.io/invocation/2a8ac8fb-c054-469d-9a46-e4fd68224a1f)
and [Codex](https://app.buildbuddy.io/invocation/171d8748-2bad-4024-b294-95edc39cdc61).
The Claude run precedes final formatting and the Codex BUILD addition; the Codex
run uses those final guest changes. These are current-working-tree focused
passes, not a wrong-route defect control, real issuer/controller validation or
a complete graph result.

Before the graph comparison, the required fixtures return to devel defaults:
four Bridge shards, ordinary kubelet sync cadence, serial Pod harness pairing,
and the 120-second Gateway reconnect deadline. The earlier larger shard count,
shorter cadence/budget and overlap trials remain documented experiments. Their
separate effects and resource costs must not be folded into a graph-only result.
