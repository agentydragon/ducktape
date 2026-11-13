# Fixture image overlap: three-suite graph trial

The [PodTemplate image-import candidate](https://github.com/agentydragon/ducktape/pull/15966)
passed a selected, concurrent Pod + MCP Actions + VM graph, but this trial does
not establish a faster CI gate. Both runs passed all 27 selected pytest cases.
The candidate's Bazel invocation took 519.987 seconds, versus 492.125 seconds
for the control. Pod was last in both, and each run overlapped different work
on the executor service. The candidate VM also received a warm retained baseline
while the control VM was cold. These are two physical samples, not a measured
latency distribution or an attributable before/after saving. The selected graph
excludes other required suites, build/lint paths, GitHub admission and checks.
PR #15966 subsequently merged at 01:24:13 UTC, head `0f039c70c3`.

## Commands, source and coverage

Both runs used `bbr test` with exactly these labels and flags:

```text
//agentplane/system_tests/suites:test_pod
//agentplane/system_tests/suites/mcp:test_actions
//agentplane/system_tests/suites:test_vm
--nocache_test_results --keep_going
```

| Run       | Bazel invocation                                                                      | Outer runner                                                                          | Actual source                                                      | Bazel elapsed |
| --------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------ | ------------: |
| Control   | [1f32be52](https://app.buildbuddy.io/invocation/1f32be52-d51a-44ff-b5e5-ab83a579cc92) | [857a85fa](https://app.buildbuddy.io/invocation/857a85fa-ecda-4601-9150-b37eb95604a5) | Exact `93f2ba8eeff36265d6e7887836b615c39b4ad985`, no patch         |      492.125s |
| Candidate | [9aa93480](https://app.buildbuddy.io/invocation/9aa93480-4b18-4073-bbe8-1d1afce70fb9) | [ba6808b2](https://app.buildbuddy.io/invocation/ba6808b2-9730-4265-871b-fa09749afed6) | Clean local code commit `54267247e728b7860546febefab75229b72b021d` |      519.987s |

The candidate remote runner fetched devel `6ea6e57fe2c4c47c57387113e772a4eff4e48192`
and cleanly applied cumulative patch
`a8aeaf750d01136d7decac3feeb80da2c62c7a0688d9cd29c4756a5ebd4ebdc4`.
That patch includes reversions of intervening devel changes. To check the
_tested_ inputs rather than trust the remote base, the three physical TestRunner
actions were decoded and their CAS input trees compared recursively. Each
control/candidate pair for the _same suite_ had the same command, platform,
timeout, and all common runfiles; the sole
different file was `agentplane/system_tests/deployment/stack.py`. Its control
SHA256 was `308fdf41c015c78e84b4ff003644d6ee3d6b55ba8f04d544eea74666e8281106`,
matching `93f2ba8`, and its candidate SHA256 was
`ba03903df400819eecf7f5c3a07f93be19dc0641cb3a128a3683f2f9d943121f`,
matching `54267247e7`. The Pod, MCP Actions and VM comparisons respectively
found 127, 128 and 103 identical CAS subtrees. This verifies those actions'
actual file inputs; it does not hash the remote checkout outside their inputs.

JUnit case-name multisets match exactly: Pod 21, MCP Actions 5, VM 1. All six
target results have zero failures, errors or skips. Both full Bazel invocations
completed successfully. Every test's persistence artifact certifies cleanup
complete and records fresh namespace, PostgreSQL Pod and root Secret UIDs. The
control's three retained baselines were cold; candidate Pod and MCP Actions
were cold, while candidate VM was warm.

## Physical execution and the limiting path

These are the returned TestRunner action records, not BuildBuddy billing or
full-CI feedback. Queue is submission to worker start; process is the remote
execution span; setup, case envelope and cleanup come from the test spans.
Case envelopes overlap internal case work, so they must not be added to summed
case durations. The decoded actions requested four compute units for Pod and
MCP Actions and eight for VM, identically within each control/candidate pair.

| Suite       | Control queue / process | Candidate queue / process | Control setup / case envelope / cleanup | Candidate setup / case envelope / cleanup | Baseline    |
| ----------- | ----------------------: | ------------------------: | --------------------------------------: | ----------------------------------------: | ----------- |
| Pod         |       0.063s / 456.482s |         0.078s / 483.949s |           152.932s / 238.967s / 34.725s |             149.724s / 263.396s / 36.540s | cold / cold |
| MCP Actions |       0.097s / 329.378s |         0.141s / 356.571s |           144.363s / 122.529s / 30.777s |             172.264s / 121.693s / 29.847s | cold / cold |
| VM          |       0.064s / 411.757s |         0.012s / 296.600s |           150.502s / 183.465s / 32.326s |              67.736s / 172.174s / 30.214s | cold / warm |

The first selected TestRunner action was queued 34.116 seconds after control
Bazel started and 34.576 seconds after candidate Bazel started. Pod's worker
finished last, at 01:10:22.439 UTC in the control and 01:20:36.698 UTC in the
candidate. The candidate's image deferral shortened Pod setup by only 3.208
seconds in this graph pair; its Pod case envelope was 24.429 seconds longer.
For MCP Actions, service-image import remained on the setup path and the
deferred images joined about 9.8 seconds after Deployments became Available;
setup took 27.901 seconds longer in this run. VM's large improvement cannot be
assigned to this code because the candidate restored a warm baseline and this
change does not defer VM images. The separate, cold focused Pod pair in #15966
measured a 14.703-second setup difference, showing the overlap benefit varies
with import and readiness timing.

Summing the three returned process spans gives 1197.617 seconds for control
and 1137.119 for candidate; observed CPU sums were 3682.955 and 3607.033
seconds. Weighting each returned execution span by its decoded compute-unit
request (Pod 4, MCP Actions 4, VM 8) gives 107.29 and 95.58 requested
CU-minutes. These sums include
parallel work and do not represent gate latency or a total cost comparison;
the VM cold/warm mismatch and provider attempt visibility limit a cost claim.

Exact TestRunner operation IDs, retained for read-only recovery:

```text
Control Pod: /uploads/7020f816-d264-4a0c-b064-3a82a2245d1e/blobs/0e9fc6254ab8dbf6359e6d6a326c8e46843bb2d90cf7e516b0bde4876ff796f4/1218
Control MCP Actions: /uploads/b913d913-dbda-4a36-9fd8-40897a7b7ca1/blobs/94b95a9a9359a021af9b13a398c14f821fabde417b9c917700ce9bfb6ac5e78e/1218
Control VM: /uploads/b13679dc-23f3-4552-b72d-9eaac2a30e60/blobs/cef225a60d1643befac28e433c1135e881ba499059e90adfda3404a011deab23/1852
Candidate Pod: /uploads/ba175449-7d7f-47e4-8781-274e72bdbc63/blobs/a21368b012e9e34d32c9b89263ad29011ea0b96c79a4dbf54d94e4c3268c4c0d/1218
Candidate MCP Actions: /uploads/334d624e-1836-4e08-b482-4566a6925c37/blobs/b6bfe8fe529858b5b36200d86e01ab92b0ba978471999a2217d0e4a5b7d1a83d/1218
Candidate VM: /uploads/643ff997-a2d1-47d1-9479-52466f592333/blobs/d8e79f9d96f615545f00040bde9704e825e9e435d871aadb0036930ec33ce337/1852
```

The [control's three operation IDs and phase records](https://app.buildbuddy.io/invocation/1f32be52-d51a-44ff-b5e5-ab83a579cc92)
and [candidate records](https://app.buildbuddy.io/invocation/9aa93480-4b18-4073-bbe8-1d1afce70fb9)
are attached to their respective invocations. The control ran from about
01:02:10 to 01:10:23 UTC and overlapped the separate image-split Pod action
until 01:04:10 UTC. The candidate ran from about 01:11:57 to 01:20:38 UTC;
the #15966 Bazel CI check started at 01:12:52 UTC and overlapped it. Worker
placement and competing load were not held fixed, and only returned action
records are visible if BuildBuddy internally retried an execution. Both outer
commands completed normally; no manual replay or cancellation was needed.

This trial preserves the selected suite coverage and demonstrates that the
candidate can run concurrently with the other long suites. It does not make
the four-minute full-gate objective: this selected graph alone took over eight
minutes in both runs. A future timing claim needs matched cold/warm and load
conditions, plus all required paths and GitHub check clocks. The merged fixture
change has a focused cold Pod setup improvement; this trial does not establish
a full-CI saving.
