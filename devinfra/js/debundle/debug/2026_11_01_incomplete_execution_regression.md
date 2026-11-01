# Incomplete execution and heap settlement

## Reproduction and candidate

#13389 tainted every object an incomplete statement read. Later own-data
member reads on those objects reported `unsupported_operation/member_read`,
abandoning otherwise modelled execution and adding conservative edges.

The synthetic CLI fixture
`incomplete_local_call_does_not_disable_later_own_data_reads` uses a benign
local spread call followed by an own-data member read. Node output is
preserved on both versions; the later read loses completeness on landed
source and remains complete with the candidate:

- Before: [ca35926c-d8f5-420c-aeef-30add30aa823](https://app.buildbuddy.io/invocation/ca35926c-d8f5-420c-aeef-30add30aa823), failed on the member read.
- Candidate: [553cdb5f-291c-4d73-918e-b36850de2f78](https://app.buildbuddy.io/invocation/553cdb5f-291c-4d73-918e-b36850de2f78), CLI fixture and six at-init settlement unit tests passed. The latter include incomplete spread calls, tagged templates and bounded recursion reaching an element before the at-init statement.

Code revision: `2f0383ee465ac00eb8aab4de04bddd67c1555774`, based on
`6e1295f7b48df04f238700212946cc4bec4a9b96`. It separates possible mutation or
escape in skipped code (`unsettled_objects`) from observed unknown-code
escape (`tainted_objects`). Only plain-array element settlement consumes
the new set. Pool-callback returns and proven Map entries retain their
unknown-code taint routes. The stale-prefix property/descriptor gap remains
the separate soundness follow-up.

## Canonical replay

Optimized build: [b53744f3-f9cf-477d-b643-b6fe68dbb639](https://app.buildbuddy.io/invocation/b53744f3-f9cf-477d-b643-b6fe68dbb639).
Engine SHA-256: `8aee6140eb9d757c520ecef1eb0c24916ec4f98a981013339f93f66c0ce7065c`.
Both replays use the same staged source/spec inputs as baseline
`b776ac78f9`, before #13389.

| Input  | Baseline edges | Candidate edges | Regressed statements | Existing rejection                                  |
| ------ | -------------: | --------------: | -------------------: | --------------------------------------------------- |
| Tana   |      2,118,225 |       2,117,853 |                    0 | Constraining SCCs 963 and 2; conditional SCC 1800   |
| Claude |      2,620,264 |       2,620,265 |                    0 | Same five rejected chunks and four atomic conflicts |

Tana removes 372 promoted eager-use edges. Claude adds one
`sequenced/cell_order` edge in `shellSandbox`; its binding, eager-use and
purity results are unchanged. Both pipeline exits remain 1 for the existing
rejections; the replays establish the comparison, not overall acceptance.

## The additional edge is redundant

Claude's only added edge is `unit:1071 -> unit:1023`, an `AfterBarrier`
relation. Both units retain their baseline purity and execution completeness.
The baseline and candidate already have these constraining edges:

```text
unit:1071 -> unit:1059  sequenced/strict_chain
unit:1059 -> unit:1023  sequenced/strict_chain
```

The new direct edge therefore adds no graph reachability or quotient ordering
requirement. All three units also share `logical:0` in the canonical spec.

The heap-frontier builder is unchanged between baseline and candidate. It
links each new barrier to the latest retained heap frontier and suppresses a
pair already emitted by another sequencing rule. Removing an intermediate
frontier at `unit:1059` exposes the direct edge while the two strict-chain
edges remain. #13394's settled global-alias refinement removing
`GlobalObjectAliasEscape` from that intermediate statement is the inferred
cause. Reports do not serialize its havoc set, so this precise intermediate
state is an inference; the redundant path is directly established from both
graphs.

The user approved publication with this documented redundant-edge exception
to the literal Claude edge-count gate. No edge was deleted or soundness
condition relaxed to meet the gate.
