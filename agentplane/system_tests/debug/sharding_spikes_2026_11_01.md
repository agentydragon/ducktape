# CI sharding spikes, 2026-11-01

## Result

MCP partitioning shortened the measured test action. VM partitioning provided little gain.
Neither establishes a twofold reduction in required PR feedback time. The unchanged Pod
suite is another roughly five-minute floor.

| Suite / variant             | Test process seconds                                 | Requested compute units |
| --------------------------- | ---------------------------------------------------- | ----------------------- |
| MCP control, three runs     | 309.7, 319.9, 308.1                                  | 4                       |
| MCP two groups, first pair  | 235.5 / 222.9; concurrent envelope 236.5             | 4 each                  |
| MCP two groups, repeat      | 191.1 / 141.3                                        | 4 each                  |
| MCP larger worker, two runs | 345.7, 248.1                                         | 8                       |
| VM control, two runs        | 338.2 / 352.2                                        | 8                       |
| VM corrected group A        | 325.0                                                | 8                       |
| VM group B, two runs        | 332.9 / 330.1                                        | 8                       |
| VM larger worker            | 411.2 successful retry, after 224.4 failed execution | 16                      |
| Unchanged Pod control       | 318.9                                                | 4                       |

These are fresh RBE test process clocks, excluding queue, input fetch and the enclosing
workflow. Every counted originating execution has invocationLinkType=1. Tests use
`--nocache_test_results`; this still permits image-store and runner reuse. No complete
required-check or auto-merge speedup was measured. VM and Pod targets remain separate;
only VM installs KubeVirt/Kyverno.

## What sharding removed

MCP has nine pytest items. Group A runs Action execution, BFF decisions, launcher ownership
and both OAuth harness cases. Group B runs discovery, policy binding and both sampler
cases. The groups are disjoint and their union equals the control's exact nine case names
in both trials. All selected items passed, with their existing assertions and cleanup.

Its serial scenario spans total about 153–162 seconds in the first two controls, versus
about 87–89 seconds on the heavier partition. The first pair saves roughly 73 seconds
relative to the first control's process time. Its combined CPU rises from 780 to 1,169
CPU-seconds. In the repeat, the partitions consume 592 CPU-seconds total, but imports are
warm: about six seconds per group versus 78 seconds in the second control. Do not attribute
the entire repeat improvement to partitioning or average away these cache differences.
The MCP larger-worker repeats also have different startup costs; compare stage receipts.

VM already overlaps six independent guests. Group A contains Codex credentials, the
central sampler and Claude turn; group B contains Claude credentials, Codex turn and OOM.
The passing groups' scenario-name union equals the control's exact six names, with no
intersection. Their Runner stage/trace checks still run. Central-sampler trace presence is
required in A; boot-device evidence is required in B, where the OOM guest generates it.
Shutdown receipts remain required for the guests each group actually creates.

Splitting does not materially shorten the slow scenarios: Claude is 189/206 seconds in
the controls and 186 seconds in corrected A; OOM is 180/194 seconds in the controls and
186 seconds in the first B run. Both groups still pay around 100 seconds of cluster setup.
A and B were also run in separate corrected submissions; their maximum process duration
is a conditional parallel-path estimate, not an observed simultaneous pair completion.

## Failed trials and provider failure

The initial submissions stopped on a PyCompile output conflict and a Ruff error. A later
MCP filter submission failed argument parsing; its larger VM control collected zero tests.
Those attempts do not enter the successful timing comparison. Unique generated Python
entrypoints avoid PyCompile conflicts when execution properties differ; explicit collection
groups avoid the pytest argument-splitting issue.

The first VM A body passed but teardown correctly rejected a missing OOM boot packet:
A does not run OOM. Corrected A does not request that packet; B retains it. The invalid
434-second trial is excluded from the passing comparison.

The ordinary 16-unit VM trial reproduced an outer RBE kernel 5.15.0 panic at
`update_load_avg+0x92/0x5c0`, without perf instrumentation. The failed physical execution
consumed 224.4 seconds / 856.9 CPU-seconds before BuildBuddy internally retried it. Its
successful retry consumed another 411.2 seconds / 2,225.5 CPU-seconds. This is not a
224-second passing suite. The retained `vm_log_tail.txt` CAS digest is
`d577cd63f01e01d3af2aaf07439b7f7a3f390339f5c90c1c5cd243369c8ba35c/48723`.
The pointer corruption's root cause remains a provider visibility boundary.

## Source and reproduction

Base: `7d755083749b74590d7fb730416c519955770422`, including the native harness feature opt-outs.
The manual spike source is commit `11d651fcebb90f9c4e19aa1e7ced214745997b44`.
The first request used transport base `58e4261128f6` plus the complete source diff; its
advertised test source CAS digests match the base commit's MCP and VM files byte for byte.
Subsequent runs use transport base `7d755083749b` plus the spike patch.

Run with the repository Nix devshell and `bbr test --nocache_test_results`:

- Controls: `//agentplane/system_tests/suites:test_mcp`, `:test_vm`, `:test_pod`.
- Partitions: `:test_mcp_spike_a`, `:test_mcp_spike_b`, `:test_vm_spike_a`, `:test_vm_spike_b`.
- Larger workers: `:test_mcp_spike_large`, `:test_vm_spike_large`.

All new variants are manual targets. Production CI selection is unchanged.
The [machine-readable receipts](sharding_spikes_2026_11_01.json) contain process/CPU clocks,
worker IDs, phase durations, case coverage and invocation links. These few runs are not
fleet percentiles or a controlled estimate holding cache and worker hardware constant.
File-scoped repository pre-commit hooks passed on the experiment source.

## Invocations

- [baseline-1](https://app.buildbuddy.io/invocation/6cc6abad-37bb-4613-8bec-125397bce4e4)
- [baseline-2](https://app.buildbuddy.io/invocation/36872bd7-57f4-469c-908a-f5d56cc6e435)
- [variants-3](https://app.buildbuddy.io/invocation/bf1386a4-3588-461e-9ce4-6c53cada8273)
- [variants-4](https://app.buildbuddy.io/invocation/050dbecc-fbdd-470c-a333-b207900067a1)
- [vm-a-corrected-1](https://app.buildbuddy.io/invocation/13c4429d-62e7-499a-a7bc-d0b60688e695)
- [split-repeat-1](https://app.buildbuddy.io/invocation/d3661c6d-c0ee-4b3a-aa64-080fd3fb4083)
- [mcp-large-1](https://app.buildbuddy.io/invocation/3082b25b-3df3-4cb9-b5fc-f765e300cb27)
- [mcp-large-2](https://app.buildbuddy.io/invocation/a9a691bf-fec2-42ba-b555-f54a6d74a822)
