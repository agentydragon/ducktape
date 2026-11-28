# Labeled control: Acorn slice

The pinned Acorn bundle's top-level unit #410 assigns `skipSpace`. Its original
`loop: while` has two nested switches; a slash that is not a comment and an
ordinary non-whitespace character both use `break loop`. The test extracts the
labeled statement without changing its bytes. In the empty host profile, the
concrete lowering census on this projection changed from `unsupported statement
LabeledStatement` to `unsupported global nonASCIIwhitespace`. The latter is a
remaining boundary, not a claim that the whole Acorn unit or bundle now lowers.
The baseline measurement used devel `51fe41664f365f9cb04c55d683f614d7ff00623e`.

The same extracted statement is also run in Node and CoreJS inside a small
receiver with explicit `charCodeAt`, `nonASCIIwhitespace`, and `String`
collaborators. These are projected calls on synthetic inputs, not calls into
the deployed Acorn package. The tests observe the resulting `pos`, including a
break from the inner switch. Separate examples check nested continues, finally,
chained labels, and iterator closing. Invalid ESTree is passed directly to the
lowerer to check target validation, including function boundaries.

The existing `Scope.same` theorem still compiles for the new statement forms;
it equates execution at every equal fuel bound for environments agreeing on
free names. This is a scope-independence result, not a claim that adding labels
costs no evaluator fuel. No deployment was constructed or checked here.
