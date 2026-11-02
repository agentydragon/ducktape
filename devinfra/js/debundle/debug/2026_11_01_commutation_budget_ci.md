# `propose-commutations` budget failure on PR #13639

The rebased PR head `eb968bb41aaf6e6e0f29efc57cf6a785b558e97f` failed only
`//devinfra/js/debundle/e2e:propose_commutations_budget_test` in its 228-test
hosted run (BuildBuddy invocation `6461aa1a-40a7-5abd-a9aa-39a2fdcef842`).
The command took 63.142852994 seconds against the test's unchanged 30-second
budget; the whole test took 169.23 seconds. The assertion failed before
checking the JSON report's proposal count, so this run does not establish
whether the command produced the expected 4094 or more candidates.

The same target passed on nearby `devel` head `8592017f203a0474d495b345c241d5f59776bae8`:
BuildBuddy invocation `953a1d20-bafb-5e3e-a559-7c278b915539` records an
81.66-second test. Its command duration is not printed in the passing test
log, but the 30-second assertion passed. BuildBuddy target history returned
217 passing entries and no failures in the retrieved page; many entries
share cached results, so they are not 217 independent measurements.

## Code-path boundary

This fixture creates 4096 alternating sequenced steps and 96 readers, adding
393216 unrelated lazy reads. `propose-commutations` receives neither
`--modules` nor `--all`, so it emits at most 50 proposals. Its current code
indexes graph edges once in `Analysis::new`, groups constraining edges by
source-unit pair in `collect_candidates`, and synthesizes declared-binding
selectors only while filling the output limit. The added
`resolved_logical_members` scan from #13617 runs when resolving an authored
commuting premise during graph construction. This fixture supplies no such
premise, and the timed command reads an already built graph. The F10 branch
changes effect coverage and refusal diagnostics, not these candidate loops.

The failure's 169.23-second total includes about 106 seconds before the timed
command returned. The prior passing run's 81.66-second total includes its
entire command plus JSON checks. Both fixture generation and command execution
can therefore contribute to the difference. Existing logs lack command phase
durations, generated graph sizes, and worker load; they do not distinguish a
graph-shape change from a slower remote worker. A 63-second command is a real
budget failure, but this evidence does not justify weakening the budget or
changing the algorithm.

## Next evidence boundary

Use the normal hosted PR CI result after the diagnostic change is integrated.
If this target fails again, compare the failing and passing runs' generated
graph node/edge counts and remote execution worker metadata, then trace the
specific changed phase using the project's documented profiler workflow.
Keep the 30-second assertion until a repeatable cause is established.
