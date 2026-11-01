# Prepared and completed purity publication

The source classifier and synchronous timing summary prove different things.
`Outcome.complete`, empty binding/heap cells and unchanged interpreter state do
not establish evaluation purity. Global/property writes, native throws and
host invocation remain residual classifier obligations until an exact existing
certificate discharges the corresponding operation. The full execution ledger
is a separate prerequisite for replacing those residuals.

## Fact boundary

`PreparedPurity` contains today's classifier Effects, including enum recognition
and prefix-resolved coercions. Five current-owner preparatory proofs explicitly
read this fact: frozen namespace admission, owner roots, closed local record
factory result, current class heritage, and closed callback-factory proof use.
The earlier class base reads only a previously completed fact.

At the end of owner execution, final assignment/export evaluation has finished,
existing local factory certificates and their dependencies have been minted,
and invoked callable heap cells have been aggregated. `complete_owner_purity`
then consumes Prepared and publishes Completed. It retains every classifier
event except an existing exact certificate's matching impure-callee call.
The completed verdict advances the prior-effect frontier before owner cleanup
and `Prefix(n+1)`. Final graph purity and at-init fallback consume Completed;
initializer-only trust still retains callback fallback roots. Vendor pruning
finalizes the same classifier without runtime refinement.

`CompletedPurity` is execution-refined classifier purity, not a claim that the
current timing Outcome provides a complete evaluation ledger. No new premise,
AST traversal, enum exemption or purity-from-completeness rule is introduced.

## Diagnostics and controls

A prepared-pure/completed-impure disagreement is stored as a sparse typed
execution report on the containing source unit. Chunk analysis emits a nonzero
count with source locations; typed graph readers and the common CLI e2e result
helper treat a disagreement as a bug. Ordinary runs add no always-zero report
mirror. Controls keep complete-timing global/property mutation impure, retain
throw/coercion obligations, and keep native purity-only rows pure despite
incomplete timing. Exact factory, initializer trust, enum and export controls
retain their existing contracts.

## Deferred owner inventory extension

The prior-effect frontier retains its established Outcome owner inventory and
checked private-constructor exclusion. Joining additional
`statement_executed_owners` is separate work: that set stores no assumption
provenance, and treating a new pool/fallback owner as an observed named setter
would be incorrect. Temporary synthetic diagnostics found no additional state
owners outside Outcome in the focused controls, so there is no demonstrated
baseline soundness failure for this union and it is excluded from this layer.
Final-value and default-export controls retain subsequent callback binding
edges. Available roots for the remainder of the current owner's value
evaluation retain their existing earlier join; only the completed prior-effect
frontier moves to finalization.

## Canonical comparison

The reviewed publication patch `570404da73` was compared with its exact base
`790219b482` on both canonical captures. Every dependency-edge array and edge
multiset matched: Tana 1/1 chunks and Claude 75/75 chunks. No statements became
pure or impure, and neither capture had a regressed or improved statement.

| Capture |     Edges | Impure statements | Rejected chunks | Atomic conflicts |
| ------- | --------: | ----------------: | --------------: | ---------------: |
| Tana    | 2,351,286 |               907 |               1 |                1 |
| Claude  | 3,384,785 |             3,555 |               6 |                5 |

These are unchanged graph/verdict observations for that exact source pair.
Tana base execution used the official project/analyze/run `--analysis` path;
the candidate used one-shot `run`. Their resource timings are not comparable
performance evidence. The failed ENOSPC attempt and immediate project-argument
failure were excluded. Comparison artifacts are
`purity-{tana,claude}-{comparison.md,edges.txt,regressions.txt}` in the cutover
scratch directory; the orchestrator retains the valid capture and saved
analysis. The later rebase preserves this patch's production changes and
integrates the existing compressed graph reader in its e2e bug gate.
