# Interpreter admission prerequisites

The interpreter prepares classifier obligations before executing a source owner
and completes its purity afterward. A current-owner admission proof cannot depend
on that completed verdict. Timing completeness, an empty access ledger, evaluation
purity and capability privacy prove different properties.

The interpreter's admission sites have these operation prerequisites. Three still
read `prepared_statement_is_pure`; lexical class heritage uses its own proof.

| Consumer                             | Required evidence                                                                                                                                                                                                                                                                                                |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `fact_roots`                         | The whole owner does not publish an executable capability. Direct function creation, class definition, imported callback factories and local record factories have different evaluation premises. Exports, eager reads, calls and writes retain their own reachability paths.                                    |
| `closed_local_record_factory_result` | Exact callee and argument evaluation; a stable synchronous local callable; initialized capture reads; getter-free fresh own-data construction and writes; no invocation or publication of retained callbacks. The fresh-record certificate also checks ordered write coverage and allocation identity.           |
| `noninvoking_class_heritage_read`    | The canonical heritage expression is exactly a lexical read of an initialized, unique ordinary source class at the settled prefix. Its class identity is neither tainted nor escaped. Whole-statement purity of either class is unnecessary.                                                                     |
| `private_callback_factory_result`    | Exact canonical initializer and direct imported callee; one inline closure argument; definite private result binding; validated retention/result summary and its provenance; prepared classifier obligations and typed dependencies. `fact_roots` and `ClosedResultRoot` reporting consume this single decision. |

## Private callback-factory results

The shared canonical `inline_callback_callee` check supplies call-site evidence of
a direct ordinary call with one non-spread syntactic function or arrow
argument. Root admission retains that evidence alongside the existing
`InitializationSourceIdentity`, including split-declarator provenance. It does not
reconstruct source eligibility from lowered values. Parenthesized initializers and
pure outer prefixes such as `(0, factory(() => later))` retain their existing
admission: the classifier
evaluates every original sequence operand. Effectful prefixes keep their ordinary
obligations. Getter/assignment/sequence callees and computed callback arguments
must not acquire the canonical direct-call evidence through value erasure.
Other classifier restrictions, including its refusal of parenthesized callees,
remain independent of the canonical call-shape evidence.

`PrivateCallbackFactoryResult` checks source/owner/binding and indexed call-site
agreement, the definite binding's exposure under the existing synchronous importer
reentry policy, and the imported summary. A closed export surface does not itself
publish the result during initialization. The proof retains
one current `PreparedPurity` premise: the callback-retention contract permits
provider-private effects and cannot establish complete initializer evaluation
freedom. Its typed dependencies include both the prefix and classifier premises.
No current `CompletedPurity`, timing completeness or operation-coverage judgment
participates in this decision.

`fact_roots` consumes the checked proof for root suppression; use reporting takes
the same proof's retained summary evidence. Reporting does not infer admission
from absence in the root set or run another purity check. A direct imported call
with one inline closure cannot also be a metadata expression statement, a global
publication assignment or a local hoisted constructor/record factory. Those exemptions keep their own decisions. Later
consumption, writes and exports retain their normal callback reachability paths.

## Ordinary lexical class heritage

`LexicalClassHeritage` is collected by the existing class-fact collection from the
canonical `extends` expression. It accepts only an identifier through parentheses.
`ValueExpr` is insufficient evidence: lowering an uncovered sequence or assignment
can leave its final binding after discarding earlier operations.

`NonInvokingClassHeritageRead` combines that source evidence with the settled
prefix's initialized binding, unique `Kind::Class` identity, earlier source owner,
and taint/escape gates. It carries the prefix's typed `ProofDependencies` into the
current operation. This is conservative prerequisite transport, not evaluation
coverage. No current prepared or completed purity query participates in this proof.

For an ordinary source class, `ClassDefinitionEvaluation`'s `IsConstructor` check
and read of the class's own non-writable, non-configurable `prototype` data property execute no
constructor, getter or method body. This permits omitting only the superclass
read's speculative callable root. The eager source read and graph ordering remain;
the declaration's whole-owner root, static work, construction and publication keep
their separate rules. A compound heritage expression receives no such proof.

The proof creates no terminal initialization certificate or compositional effect
fragment, changes no earlier prefix, and supplies no new purity refinement. The
vendor-prune classifier is unchanged. Native integrity, allocation multiplicity,
callback capture initialization and observed/assumed effect tags retain their
existing owners.
