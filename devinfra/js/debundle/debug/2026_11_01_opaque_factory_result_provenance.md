# Opaque results and audited factory input provenance

## Reproduction

`foreign_call_summary_test::opaque_result_does_not_certify_an_audited_factory_input`
wraps an accessor-bearing input with a source-derived foreign factory, then
passes that opaque result to an explicitly audited factory. The outer contract
requires an ordinary accessor-free input graph. A callback-retention carrier
does not establish this precondition. Before separating provenance, the CLI
accepted a split that changes a caught temporal-dead-zone read into `42`.

The pre-separation carrier patch failed this test in BuildBuddy invocation
`e907d4af-d5de-4704-b151-7e20d4b52d52`. That patch already retained full live input
values; retaining those values alone does not fix the admission error.

## Producers

| Producer                                                        | Result carrier                                            |
| --------------------------------------------------------------- | --------------------------------------------------------- |
| Source-derived foreign fresh allocation                         | Opaque `DeferredCallbacks`, retaining actual input values |
| Native/collection/fromEntries/source-derived constructor result | Opaque `DeferredCallbacks`                                |
| Existing retained factory contract                              | Revocable `AuditedRetainedResult(Factory, ...)`           |
| Existing source-proved or asserted callback factory             | Revocable `AuditedRetainedResult(Factory, ...)`           |
| Existing inline callback factory contract                       | Revocable `AuditedRetainedResult(Factory, ...)`           |
| Member projection or invocation of either carrier               | Opaque `DeferredCallbacks`                                |

The new internal capability consumes the existing exact source proof or author
contract. It adds no provider assertion, public summary schema, effect ledger,
purity fact, or no-publication premise.

## Reader audit

| Reader                                                               | Rule                                                                                         |
| -------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| Stored-reference visitor, callback extraction and callback promotion | Traverse both carriers and retain all represented references                                 |
| Factory argument admission                                           | Require unrevoked factory provenance and existing callback taint/escape checks               |
| Member reads and calls                                               | Preserve callback reach; do not transfer graph eligibility                                   |
| Writes, unknown exposure and skipped execution                       | Revoke through the shared taint/unsettle traversal, including nested carrier references      |
| Heap reachability                                                    | Revoked carriers require global reach; retained input taints/unknowns keep existing widening |
| Invocation and callback-pool caches                                  | Revocation increments prefix revision and participates in the pool observation               |
| Abstract equality/ordering                                           | Capture key is a cache key, never JavaScript object equality or fresh allocation identity    |
| Native shape/ordinary-data/constructor guards                        | Neither carrier proves an ordinary own-data shape                                            |
| Runtime callback dispatcher                                          | Delegates to these same interpreter transfers                                                |

Revocation keys are interned capture sets. Distinct results can share a key, so
one exposure may disqualify another result conservatively. Nothing clears that
revocation or uses key equality to prove runtime aliases or privacy.

## Precision cost and controls

Composition through generic foreign/native/collection/source-derived constructor results and
member/call results becomes conservative. Recognized primitive result writes
can also lose composition eligibility while preserving the existing callback
deferral rule. Existing direct audited factory and constructor chains remain supported. Constructor provenance cannot certify ordinary factory inputs; its contract permits provider-private prototype helpers.

Alias, cycle, later Array/Map/Set insertion and equality/shape fixtures are
preservation controls: the old broad fallback may already protect them. The
opaque input admission fixture is the observed failing case. Mutation controls
cover an opaque provider installing a getter, direct `defineProperty`, nested
accessor insertion and prototype replacement before later factory reuse.
No corpus acceptance or edge-count improvement is claimed.
