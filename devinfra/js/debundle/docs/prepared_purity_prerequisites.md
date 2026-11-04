# Interpreter admission prerequisites

The interpreter prepares classifier obligations before executing a source owner
and completes its purity afterward. A current-owner admission proof cannot depend
on that completed verdict. Timing completeness, an empty access ledger, evaluation
purity and capability privacy prove different properties.

The interpreter's admission sites have these operation prerequisites. Four still
read `prepared_statement_is_pure`; lexical class heritage uses its own proof.

| Consumer                                | Required evidence                                                                                                                                                                                                                                                                                                                    |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `private_frozen_namespace`              | Complete initializer syntax; intact `Object.freeze`, `Object.defineProperty` and `Symbol.toStringTag`; getter-free ordinary literal inputs; initialized payload reads; non-throwing descriptor application and freezing; mutation confined to the fresh private object. Stored callbacks stay lazy until publication or consumption. |
| `fact_roots`                            | The whole owner does not publish an executable capability. Direct function creation, class definition, imported callback factories and local record factories have different evaluation premises. Exports, eager reads, calls and writes retain their own reachability paths.                                                        |
| `closed_local_record_factory_result`    | Exact callee and argument evaluation; a stable synchronous local callable; initialized capture reads; getter-free fresh own-data construction and writes; no invocation or publication of retained callbacks. The fresh-record certificate also checks ordered write coverage and allocation identity.                               |
| `noninvoking_class_heritage_read`       | The canonical heritage expression is exactly a lexical read of an initialized, unique ordinary source class at the settled prefix. Its class identity is neither tainted nor escaped. Whole-statement purity of either class is unnecessary.                                                                                         |
| `ClosedResultRoot` callback-factory use | The imported factory's validated retention/result contract and its provenance, plus the same successful whole-owner non-publication decision used by `fact_roots`. Recording a use does not itself establish evaluation freedom.                                                                                                     |

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
